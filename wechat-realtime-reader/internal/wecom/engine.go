package wecom

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

type Options struct {
	Root, PlatformRoot, Container, App string
	// Snapshot and Account select an explicit offline fixture, never auto-discovered.
	Snapshot, Account string
}

type Engine struct {
	mu, busy        sync.Mutex
	outputMu        sync.Mutex
	ctx             context.Context
	cancel          context.CancelFunc
	workers         sync.WaitGroup
	output          io.Writer
	querySlots      chan struct{}
	options         Options
	cache, runtime  string
	source, current string
	accountOverride string
	signature       signature
	updated         any
	chosen          string
	lastContext     object
	keysByAccount   map[string]map[string]string
	pendingAccount  string
	candidates      []string
	capture         *captureProcess
	captureSince    time.Time
	attachedPID     int
	attemptedPID    int
	managedPID      int
	clientChecked   int
	// Overridden only by in-package tests.
	processes func() []int
	discover  func([]int) (string, error)
}

func New(options Options, output io.Writer) (*Engine, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return nil, err
	}
	if options.Root == "" {
		options.Root = os.Getenv("WF_WECOM_ROOT")
		if options.Root == "" {
			options.Root = filepath.Join(home, "Library/Application Support/WeComCompanion")
		}
	}
	if options.Container == "" {
		options.Container = filepath.Join(home, "Library/Containers/com.tencent.WeWorkMac/Data")
	}
	if options.App == "" {
		options.App = "/Applications/企业微信.app"
	}
	if options.PlatformRoot == "" {
		options.PlatformRoot = os.Getenv("WF_PLATFORM_ROOT")
		if options.PlatformRoot == "" {
			binary, err := os.Executable()
			if err != nil {
				return nil, err
			}
			options.PlatformRoot = filepath.Dir(filepath.Dir(filepath.Dir(binary)))
		}
	}
	if err := os.MkdirAll(options.Root, 0700); err != nil {
		return nil, err
	}
	cache, err := os.MkdirTemp(options.Root, "snapshot-")
	if err != nil {
		return nil, err
	}
	ctx, cancel := context.WithCancel(context.Background())
	e := &Engine{ctx: ctx, cancel: cancel, output: output, options: options, cache: cache,
		runtime:       filepath.Join(options.Root, "Runtime/企业微信.app"),
		keysByAccount: map[string]map[string]string{}, processes: clientProcesses, querySlots: make(chan struct{}, 64)}
	e.discover = func(pids []int) (string, error) { return discover(pids, options.Container, openPaths) }
	if options.Snapshot != "" {
		if options.Account == "" {
			e.Close()
			return nil, fmt.Errorf("离线快照必须指定账号")
		}
		// Offline input receives the same integrity/read-only snapshot boundary.
		sig, err := snapshot(options.Snapshot, nil, filepath.Join(cache, "offline"))
		if err != nil {
			e.Close()
			return nil, err
		}
		e.source, e.current = options.Snapshot, filepath.Join(cache, "offline")
		e.accountOverride, e.signature = options.Account, sig
		e.updated = float64(time.Now().UnixNano()) / 1e9
		e.emit("account", object{"account": e.account()})
		e.state("connected", "离线快照 · 验证模式", "仅读取显式指定的离线数据，不发现或操作企业微信客户端。")
	} else {
		e.state("starting", "正在发现企业微信", "无需输入目录、进程号或密钥。")
	}
	return e, nil
}

func (e *Engine) emit(kind string, payload object) {
	event := object{"event": kind}
	for key, value := range payload {
		event[key] = value
	}
	e.outputMu.Lock()
	defer e.outputMu.Unlock()
	json.NewEncoder(e.output).Encode(event)
}

func (e *Engine) state(code, title, detail string) {
	e.emit("status", object{"code": code, "title": title, "detail": detail,
		"updated": e.updated, "managed": e.managedPID != 0})
}

func (e *Engine) account() string {
	if e.accountOverride != "" {
		return e.accountOverride
	}
	if e.source == "" {
		return ""
	}
	hash := sha256.Sum256([]byte(e.source))
	return hex.EncodeToString(hash[:])
}

// Caller holds mu; queries can never observe a stale validated snapshot.
func (e *Engine) clearContext() {
	old := e.current
	e.current, e.chosen = "", ""
	e.signature, e.lastContext, e.updated = nil, nil, nil
	if old != "" {
		os.RemoveAll(old)
	}
	e.emit("context", object{"sessions": []object{}, "selected": nil, "messages": []object{}, "summary": "", "todos": []string{}})
}

func (e *Engine) tryCandidates() (map[string]string, error) {
	result := map[string]string{}
	if e.source == "" {
		return result, nil
	}
	for _, name := range dbNames {
		f, err := os.Open(filepath.Join(e.source, name))
		if err != nil {
			return nil, err
		}
		header := make([]byte, 24)
		_, err = io.ReadFull(f, header)
		size, sizeErr := pageLength(header)
		if err != nil || sizeErr != nil {
			f.Close()
			return nil, fmt.Errorf("数据库头不完整或格式尚未支持")
		}
		f.Seek(0, 0)
		first := make([]byte, size)
		_, err = io.ReadFull(f, first)
		f.Close()
		if err != nil {
			return nil, fmt.Errorf("数据库页不完整")
		}
		if bytes.HasPrefix(first, magic) {
			result[name] = ""
			continue
		}
		existing := e.keysByAccount[e.account()][name]
		candidates := append([]string{existing}, e.candidates...)
		for _, key := range candidates {
			raw, err := hex.DecodeString(key)
			if err != nil {
				continue
			}
			if _, err := decodePage(first, raw, 1); err == nil {
				result[name] = key
				break
			}
		}
	}
	if len(result) == 3 && !reflect.DeepEqual(result, e.keysByAccount[e.account()]) {
		e.keysByAccount[e.account()] = result
		e.emit("keys", object{"account": e.account(), "keys": result})
	}
	return result, nil
}

func (e *Engine) sync() error {
	e.mu.Lock()
	defer e.mu.Unlock()
	pids := e.processes()
	if len(pids) != 1 {
		e.clearContext()
		e.source, e.pendingAccount = "", ""
		if len(pids) == 0 {
			e.attemptedPID, e.managedPID, e.clientChecked, e.attachedPID = 0, 0, 0, 0
			if e.capture != nil {
				e.capture.stop()
				e.capture = nil
			}
			e.emit("managed", object{"pid": nil})
			e.state("client_missing", "等待企业微信登录", "打开并登录本机企业微信后，伴侣会自动继续连接。")
		} else {
			e.state("ambiguous", "检测到多个企业微信进程", "请保留一个客户端后重试，避免读取错误账号。")
		}
		return nil
	}
	pid := pids[0]
	if e.clientChecked != pid {
		e.clientChecked = pid
		if executable, err := run(e.ctx, 4*time.Second, "/bin/ps", "-p", strconv.Itoa(pid), "-o", "comm="); err == nil {
			e.managedPID = 0
			if strings.TrimSpace(string(executable)) == filepath.Join(e.runtime, "Contents/MacOS/企业微信") {
				e.managedPID = pid
			}
			e.emit("managed", object{"pid": nullablePID(e.managedPID)})
		}
	}
	e.state("discovering", "正在识别当前账号", "按正在运行的客户端识别数据；历史账号不会自动选中。")
	source, err := e.discover(pids)
	if err != nil {
		e.clearContext()
		e.source, e.pendingAccount = "", ""
		if os.IsPermission(err) {
			return err
		}
		e.state("account_wait", "等待确认当前账号", "检测到多个正在使用的数据目录，请在企业微信中保留一个登录账号后重试")
		return nil
	}
	if source == "" {
		e.clearContext()
		e.source, e.pendingAccount = "", ""
		e.state("account_wait", "等待数据库就绪", "请在企业微信登录并打开一个会话。")
		return nil
	}
	if source != e.source {
		e.clearContext()
		e.source, e.pendingAccount = source, ""
		e.pendingAccount = e.account()
		e.emit("account", object{"account": e.account()})
	}
	keys, err := e.tryCandidates()
	if err != nil {
		return err
	}
	if len(keys) < 3 {
		e.clearContext()
		if e.pendingAccount == e.account() {
			e.state("restoring", "正在恢复已保存的连接", "正在读取钥匙串；若系统提示，请允许本应用访问已保存的密钥。")
			return nil
		}
		if e.attemptedPID != pid {
			e.attemptedPID = pid
			e.state("attaching", "正在连接取钥组件", "系统保护可能要求连接授权；不会自动重启企业微信。")
			if err := e.attach(pid); err != nil {
				e.state("approval", "需要一次客户端连接授权", "当前客户端的系统保护阻止自动取钥。可在授权后尝试临时运行副本；原安装和 SIP 保持不变。")
				return nil
			}
		}
		if e.attachedPID != pid {
			e.state("approval", "需要一次客户端连接授权", "没有取得可用密钥。首次连接按钮会说明临时副本和客户端重启步骤。")
		} else if time.Since(e.captureSince) < 60*time.Second {
			e.state("capturing", "正在自动验证密钥", fmt.Sprintf("已验证 %d/3 个数据库。请在企业微信打开或切换会话；未验证时不会展示历史缓存。", len(keys)))
		} else {
			e.state("capture_wait", "尚未捕获有效密钥", fmt.Sprintf("已验证 %d/3 个数据库。请在企业微信打开或切换会话；未验证时不会展示历史缓存。", len(keys)))
		}
		return nil
	}
	sig, err := fingerprints(source)
	if err != nil {
		return err
	}
	if !reflect.DeepEqual(sig, e.signature) || e.current == "" {
		fresh := filepath.Join(e.cache, strconv.FormatInt(time.Now().UnixNano(), 10))
		sig, err = snapshot(source, keys, fresh)
		if err != nil {
			return err
		}
		context, err := buildContext(fresh, e.chosen)
		if err != nil {
			os.RemoveAll(fresh)
			return err
		}
		old := e.current
		e.current, e.signature, e.lastContext = fresh, sig, context
		e.updated = float64(time.Now().UnixNano()) / 1e9
		if old != "" {
			os.RemoveAll(old)
		}
		payload := object{"updated": e.updated}
		for key, value := range context {
			payload[key] = value
		}
		e.emit("context", payload)
	}
	e.state("connected", "已连接 · 自动同步", "正在读取当前账号的本地快照。上下文为所选会话或最近活跃会话，不代表当前前台窗口。")
	return nil
}

func (e *Engine) Start() {
	if e.options.Snapshot != "" {
		return
	}
	e.workers.Add(1)
	go func() {
		defer e.workers.Done()
		ticker := time.NewTicker(5 * time.Second)
		defer ticker.Stop()
		for {
			if e.busy.TryLock() {
				err := e.sync()
				if err != nil {
					e.mu.Lock()
					e.clearContext()
					if os.IsPermission(err) {
						e.state("permission", "需要允许读取本地数据", "macOS 阻止访问企业微信数据。请通过“允许读取目录”授予访问后重试。")
					} else {
						e.state("read_error", "正在等待可用快照", "读取暂未完成；请检查登录状态、数据库格式与 macOS 文件访问权限。")
					}
					e.mu.Unlock()
				}
				e.busy.Unlock()
			}
			select {
			case <-e.ctx.Done():
				return
			case <-ticker.C:
			}
		}
	}()
}

var validKey = regexp.MustCompile(`^[0-9a-f]{32}$`)

func (e *Engine) Action(command object) {
	switch str(command["op"]) {
	case "keys":
		account, ok := command["account"].(string)
		keys, keysOK := command["keys"].(map[string]any)
		if !ok || !keysOK {
			return
		}
		valid := map[string]string{}
		for _, name := range dbNames {
			if k, ok := keys[name].(string); ok && (k == "" || validKey.MatchString(k)) {
				valid[name] = k
			}
		}
		e.mu.Lock()
		if len(valid) > 0 || e.keysByAccount[account] == nil {
			e.keysByAccount[account] = valid
		}
		if e.pendingAccount == account {
			e.pendingAccount = ""
		}
		e.mu.Unlock()
	case "prepare", "start_runtime":
		if e.options.Snapshot != "" {
			e.emit("notice", object{"text": "离线模式不操作客户端。"})
			return
		}
		if !e.busy.TryLock() {
			e.emit("notice", object{"text": "正在连接，请稍后重试首次连接。"})
			return
		}
		e.workers.Add(1)
		go func() {
			defer e.workers.Done()
			defer e.busy.Unlock()
			if command["op"] == "prepare" {
				e.prepareRuntime()
			} else {
				e.startRuntime()
			}
		}()
	case "restored":
		e.mu.Lock()
		if e.capture != nil {
			e.capture.stop()
			e.capture = nil
		}
		e.managedPID, e.attachedPID, e.attemptedPID, e.clientChecked = 0, 0, 0, 0
		e.clearContext()
		e.mu.Unlock()
	case "quit":
		e.cancel()
	}
}

func (e *Engine) Respond(command object) {
	requestID, ok := command["request_id"].(string)
	if !ok || len(requestID) > 100 {
		return
	}
	select {
	case e.querySlots <- struct{}{}:
	default:
		e.emit("response", object{"request_id": requestID, "ok": false, "status": 503, "error": "读取请求较多，请稍后重试"})
		return
	}
	// Keep stdin responsive to quit/restored while a snapshot or copy is busy.
	e.workers.Add(1)
	go func() {
		defer e.workers.Done()
		defer func() { <-e.querySlots }()
		params, _ := command["params"].(map[string]any)
		data, err := e.Query(str(command["path"]), params)
		if err != nil {
			status, text := failure(err)
			e.emit("response", object{"request_id": requestID, "ok": false, "status": status, "error": text})
			return
		}
		e.emit("response", object{"request_id": requestID, "ok": true, "data": data})
	}()
}

func (e *Engine) Handle(command object) {
	op := str(command["op"])
	if op == "query" {
		e.Respond(command)
	} else if op == "keys" || op == "restored" {
		// These actions may wait for the snapshot lock; quit must still be consumed.
		e.workers.Add(1)
		go func() {
			defer e.workers.Done()
			e.Action(command)
		}()
	} else {
		e.Action(command)
	}
}

func (e *Engine) Close() {
	e.cancel()
	e.workers.Wait()
	os.RemoveAll(e.cache)
}

func run(ctx context.Context, timeout time.Duration, command string, args ...string) ([]byte, error) {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	// stderr can contain paths; keep it private rather than emitting it to the pipe.
	return exec.CommandContext(ctx, command, args...).Output()
}

func clientProcesses() []int {
	output, err := run(context.Background(), 4*time.Second, "/usr/bin/pgrep", "-x", "企业微信")
	if err != nil {
		return nil
	}
	pids := []int{}
	for _, word := range strings.Fields(string(output)) {
		if n, err := strconv.Atoi(word); err == nil {
			pids = append(pids, n)
		}
	}
	return pids
}

func openPaths(pid int) []string {
	output, err := run(context.Background(), 8*time.Second, "/usr/sbin/lsof", "-p", strconv.Itoa(pid), "-Fn")
	if err != nil {
		return nil
	}
	paths := []string{}
	for _, line := range strings.Split(string(output), "\n") {
		if strings.HasPrefix(line, "n") {
			paths = append(paths, line[1:])
		}
	}
	return paths
}

func boundedDatasets(root string) ([]string, error) {
	found := []string{}
	skip := map[string]bool{"Avator": true, "CacheMapping": true, "Emotion": true, "File": true,
		"Video": true, "Image": true, "Log": true, "Cef": true}
	err := filepath.WalkDir(root, func(path string, entry os.DirEntry, err error) error {
		if os.IsNotExist(err) {
			return nil
		}
		if err != nil {
			return err
		}
		if entry == nil || !entry.IsDir() {
			return nil
		}
		relative, _ := filepath.Rel(root, path)
		if path != root && (skip[entry.Name()] || len(strings.Split(relative, string(os.PathSeparator))) > 4) {
			return filepath.SkipDir
		}
		all := true
		for _, name := range dbNames {
			st, err := os.Stat(filepath.Join(path, name))
			if os.IsPermission(err) {
				return err
			}
			all = all && err == nil && !st.IsDir()
		}
		if all {
			found = append(found, path)
			return filepath.SkipDir
		}
		return nil
	})
	return found, err
}

var accountPath = regexp.MustCompile(`/WXWork/Data/([^/]+)/`)

func discover(pids []int, container string, paths func(int) []string) (string, error) {
	direct, accounts := map[string]bool{}, map[string]bool{}
	for _, pid := range pids {
		for _, path := range paths(pid) {
			for _, name := range dbNames {
				if filepath.Base(path) == name {
					direct[filepath.Dir(path)] = true
				}
			}
			if match := accountPath.FindStringSubmatch(path); len(match) == 2 {
				accounts[match[1]] = true
			}
		}
	}
	if len(direct) == 1 {
		for source := range direct {
			return source, nil
		}
	}
	if len(direct) > 1 {
		return "", fmt.Errorf("检测到多个正在使用的数据目录")
	}
	if len(accounts) == 0 {
		return "", nil
	}
	active := map[string]bool{}
	ids := []string{}
	for account := range accounts {
		ids = append(ids, account)
	}
	sort.Strings(ids)
	for _, account := range ids {
		folders, err := boundedDatasets(filepath.Join(container, "Library/Application Support/WXWork/Data", account))
		if err != nil {
			return "", err
		}
		for _, folder := range folders {
			active[folder] = true
		}
	}
	if len(active) > 1 {
		return "", fmt.Errorf("检测到多个正在使用的数据目录")
	}
	for folder := range active {
		return folder, nil
	}
	return "", nil
}

func nullablePID(pid int) any {
	if pid == 0 {
		return nil
	}
	return pid
}
