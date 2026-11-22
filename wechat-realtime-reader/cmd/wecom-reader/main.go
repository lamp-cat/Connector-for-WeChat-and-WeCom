// wecom-reader uses inherited NDJSON pipes only; there is no network listener.
package main

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"syscall"

	"github.com/missuo/wechat-realtime-reader/internal/wecom"
)

func main() {
	syscall.Umask(0077)
	var options wecom.Options
	flag.StringVar(&options.Snapshot, "snapshot", "", "explicit offline SQLite dataset (validation only)")
	flag.StringVar(&options.Account, "account", "", "account identity for the offline dataset")
	flag.Parse()
	engine, err := wecom.New(options, os.Stdout)
	if err != nil {
		fmt.Fprintln(os.Stderr, "企业微信读取器初始化未完成")
		os.Exit(1)
	}
	defer engine.Close()
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGTERM, syscall.SIGINT)
	defer signal.Stop(signals)
	go func() {
		<-signals
		// Unblock stdin as well as cancel any capture/preparation subprocess.
		os.Stdin.Close()
		engine.Action(map[string]any{"op": "quit"})
	}()
	engine.Start()
	scanner := bufio.NewScanner(os.Stdin)
	scanner.Buffer(make([]byte, 4096), 65536)
	for scanner.Scan() {
		var command map[string]any
		if json.Unmarshal(scanner.Bytes(), &command) != nil {
			continue
		}
		engine.Handle(command)
		if command["op"] == "quit" {
			break
		}
	}
}
