package wecom

import (
	"bufio"
	"bytes"
	"context"
	_ "embed"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"syscall"
	"time"
)

//go:embed hook.js
var hookScript []byte

type captureProcess struct {
	cancel context.CancelFunc
	stdin  io.WriteCloser
}

func (c *captureProcess) stop() {
	c.cancel()
	c.stdin.Close()
}

// Caller holds mu. stdout is consumed independently of event handling, so a
// candidate arriving before "installed" cannot deadlock the readiness handshake.
func (e *Engine) attach(pid int) error {
	if e.capture != nil {
		e.capture.stop()
		e.capture = nil
		e.attachedPID = 0
	}
	arch := runtime.GOARCH
	if arch == "amd64" {
		arch = "x86_64"
	}
	inject := filepath.Join(e.options.PlatformRoot, "wechat-realtime-reader/skills/wechat-db-keys/vendor/frida/frida-inject-"+arch)
	script := filepath.Join(e.cache, "capture-"+strconv.FormatInt(time.Now().UnixNano(), 10)+".js")
	if err := os.WriteFile(script, hookScript, 0600); err != nil {
		return err
	}
	ctx, cancel := context.WithCancel(e.ctx)
	command := exec.CommandContext(ctx, inject, "-p", strconv.Itoa(pid), "-s", script)
	stdin, err := command.StdinPipe()
	if err != nil {
		cancel()
		os.Remove(script)
		return err
	}
	stdout, err := command.StdoutPipe()
	if err != nil {
		stdin.Close()
		cancel()
		os.Remove(script)
		return err
	}
	command.Stderr = io.Discard
	if err := command.Start(); err != nil {
		stdin.Close()
		cancel()
		os.Remove(script)
		return err
	}
	capture := &captureProcess{cancel: cancel, stdin: stdin}
	e.capture = capture
	ready := make(chan bool, 1)
	done := make(chan struct{})
	events := make(chan object, 256)
	e.workers.Add(2)
	go func() {
		defer e.workers.Done()
		defer close(events)
		defer close(done)
		defer os.Remove(script)
		defer stdin.Close()
		scanner := bufio.NewScanner(stdout)
		scanner.Buffer(make([]byte, 4096), 1<<20)
		for scanner.Scan() {
			var event object
			if json.Unmarshal(scanner.Bytes(), &event) != nil {
				continue
			}
			if event["type"] == "send" {
				if payload, ok := event["payload"].(map[string]any); ok {
					event = payload
				}
			}
			if event["type"] == "installed" {
				select {
				case ready <- number(event["count"]) > 0:
				default:
				}
			}
			select {
			case events <- event:
			case <-ctx.Done():
			}
		}
		command.Wait()
	}()
	go func() {
		defer e.workers.Done()
		for event := range events {
			e.mu.Lock()
			if e.capture == capture {
				switch str(event["type"]) {
				case "candidate":
					value := str(event["value"])
					if validKey.MatchString(value) {
						exists := false
						for _, old := range e.candidates {
							exists = exists || old == value
						}
						if !exists {
							e.candidates = append(e.candidates, value)
							e.candidates = e.candidates[max(0, len(e.candidates)-128):]
							e.emit("capture_state", object{"candidate_count": len(e.candidates)})
						}
					}
				case "installed":
					e.emit("capture_state", object{"hooks": event["count"]})
				case "activity":
					e.emit("capture_state", object{"calls": event["calls"]})
				}
			}
			e.mu.Unlock()
		}
		e.mu.Lock()
		if e.capture == capture {
			e.attachedPID = 0
		}
		e.mu.Unlock()
	}()
	fail := func() error {
		capture.stop()
		e.capture = nil
		return fmt.Errorf("取钥监听未完成")
	}
	timer := time.NewTimer(15 * time.Second)
	defer timer.Stop()
	select {
	case ok := <-ready:
		if !ok {
			return fail()
		}
		e.attachedPID, e.captureSince = pid, time.Now()
		return nil
	case <-done:
		return fail()
	case <-timer.C:
		return fail()
	case <-e.ctx.Done():
		return fail()
	}
}

func (e *Engine) prepareRuntime() {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.state("preparing", "正在准备临时运行副本", "仅复制并签名独立副本；此步骤不会退出或启动企业微信。")
	err := e.prepareCopy()
	if err != nil {
		e.state("prepare_error", "临时副本未能准备完成", "原客户端未改动。可重试；当前版本尚不能直接完成自动取钥。")
		return
	}
	e.emit("runtime_ready", object{"detail": "临时副本已准备。继续连接会正常退出当前企业微信，并在取钥监听启动后运行副本。此兼容性仍需实机验证。"})
}

func (e *Engine) prepareCopy() error {
	info := filepath.Join(e.options.App, "Contents/Info.plist")
	build, err := run(e.ctx, 15*time.Second, "/usr/bin/plutil", "-extract", "CFBundleVersion", "raw", "-o", "-", info)
	if err != nil {
		return err
	}
	version, err := run(e.ctx, 15*time.Second, "/usr/bin/plutil", "-extract", "CFBundleShortVersionString", "raw", "-o", "-", info)
	if err != nil {
		return err
	}
	value := string(build) + "-" + string(version)
	parent := filepath.Dir(e.runtime)
	marker := filepath.Join(parent, "version.txt")
	if saved, err := os.ReadFile(marker); err == nil && string(saved) == value {
		_, err := run(e.ctx, 90*time.Second, "/usr/bin/codesign", "--verify", "--deep", "--strict", e.runtime)
		return err
	}
	if err := os.MkdirAll(parent, 0700); err != nil {
		return err
	}
	staging := filepath.Join(parent, "staging-"+strconv.FormatInt(time.Now().UnixNano(), 10)+".app")
	defer os.RemoveAll(staging)
	if _, err := run(e.ctx, 300*time.Second, "/usr/bin/ditto", e.options.App, staging); err != nil {
		return err
	}
	entitlements := filepath.Join(parent, "runtime-entitlements.plist")
	if err := os.WriteFile(entitlements, []byte(`<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>com.apple.security.get-task-allow</key><true/>
<key>com.apple.security.cs.allow-jit</key><true/>
<key>com.apple.security.cs.allow-unsigned-executable-memory</key><true/>
<key>com.apple.security.cs.disable-library-validation</key><true/>
</dict></plist>`), 0600); err != nil {
		return err
	}
	if _, err := run(e.ctx, 240*time.Second, "/usr/bin/codesign", "--force", "--deep", "--sign", "-", "--entitlements", entitlements, staging); err != nil {
		return err
	}
	if _, err := run(e.ctx, 90*time.Second, "/usr/bin/codesign", "--verify", "--deep", "--strict", staging); err != nil {
		return err
	}
	if err := os.RemoveAll(e.runtime); err != nil {
		return err
	}
	if err := os.Rename(staging, e.runtime); err != nil {
		return err
	}
	return os.WriteFile(marker, []byte(value), 0600)
}

func (e *Engine) startRuntime() {
	e.mu.Lock()
	defer e.mu.Unlock()
	if len(e.processes()) != 0 {
		e.state("runtime_error", "临时副本连接未完成", "企业微信尚未正常退出，已取消副本启动；没有强制结束客户端。")
		return
	}
	helper := filepath.Join(e.options.PlatformRoot, "setup/wecom-runtime")
	ctx, cancel := context.WithTimeout(e.ctx, 15*time.Second)
	defer cancel()
	command := exec.CommandContext(ctx, helper)
	payload, _ := json.Marshal(object{"executable": filepath.Join(e.runtime, "Contents/MacOS/企业微信"), "container": e.options.Container})
	command.Stdin = bytes.NewReader(payload)
	output, err := command.Output()
	var result struct {
		PID int `json:"pid"`
	}
	if err != nil || json.Unmarshal(output, &result) != nil || result.PID <= 0 {
		e.state("runtime_error", "临时副本连接未完成", "临时副本尚未准备或启动未完成。请重试连接或恢复官方客户端。")
		return
	}
	// Never leave the user's client suspended, including on cancellation/failure.
	process, _ := os.FindProcess(result.PID)
	defer process.Signal(syscall.SIGCONT)
	e.managedPID = result.PID
	e.emit("managed", object{"pid": result.PID})
	if err := e.attach(result.PID); err != nil {
		e.state("runtime_error", "临时副本连接未完成", "取钥监听未就绪。此客户端版本或系统可能不兼容临时副本，请使用“恢复官方客户端”。")
		return
	}
	e.attemptedPID = result.PID
	e.state("capturing", "正在连接临时运行副本", "若需要请在企业微信登录。伴侣会自动捕获并逐库验证密钥。")
}
