package wecom

import (
	"bytes"
	"crypto/cipher"
	"database/sql"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"strings"
	"testing"
)

var testKey, _ = hex.DecodeString("102030405060708090a0b0c0d0e0f001")

func must(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}

func datasets(t *testing.T, folder string) {
	t.Helper()
	must(t, os.MkdirAll(folder, 0700))
	for name, sqlText := range map[string]string{
		"message.db": `CREATE TABLE message_table(conversation_id TEXT,message_id TEXT,send_time REAL,content,sender_id TEXT,content_type INTEGER);
INSERT INTO message_table VALUES('S1','10',1795200000000,'请明天确认交付时间','U1',NULL);
INSERT INTO message_table VALUES('S2','11',1795199999000,'旧会话消息','U2',NULL);`,
		"session.db": `CREATE TABLE conversation_table(id TEXT,name TEXT,last_message_time INTEGER);
INSERT INTO conversation_table VALUES('S1','当前讨论',0),('S2','旧讨论',0);`,
		"user.db": `CREATE TABLE user_table(id TEXT,name TEXT);
INSERT INTO user_table VALUES('U1','同事');`,
	} {
		db, err := sql.Open("sqlite", filepath.Join(folder, name))
		must(t, err)
		_, err = db.Exec("PRAGMA page_size=4096;" + sqlText)
		must(t, err)
		must(t, db.Close())
	}
}

func encryptPage(t *testing.T, plain []byte, page uint32) []byte {
	block, iv, err := pageCipher(testKey, page)
	must(t, err)
	if page > 1 {
		output := make([]byte, len(plain))
		cipher.NewCBCEncrypter(block, iv).CryptBlocks(output, plain)
		return output
	}
	body := make([]byte, len(plain)-16)
	cipher.NewCBCEncrypter(block, iv).CryptBlocks(body, plain[16:])
	return append(append(append([]byte("fixture!"), body[:8]...), plain[16:24]...), body[8:]...)
}

func encryptDataset(t *testing.T, folder string) {
	for _, name := range dbNames {
		path := filepath.Join(folder, name)
		raw, err := os.ReadFile(path)
		must(t, err)
		for i := 0; i < len(raw); i += 4096 {
			copy(raw[i:], encryptPage(t, raw[i:i+4096], uint32(i/4096+1)))
		}
		must(t, os.WriteFile(path, raw, 0600))
	}
}

type walFrame struct {
	page, commit uint32
	body         []byte
}

func journal(t *testing.T, salt string, frames ...walFrame) []byte {
	header := make([]byte, 32)
	binary.BigEndian.PutUint32(header, 0x377f0682)
	binary.BigEndian.PutUint32(header[4:], 3007000)
	binary.BigEndian.PutUint32(header[8:], 4096)
	copy(header[16:], salt)
	state, err := checksum(header[:24], [2]uint32{}, binary.LittleEndian)
	must(t, err)
	binary.BigEndian.PutUint32(header[24:], state[0])
	binary.BigEndian.PutUint32(header[28:], state[1])
	output := header
	for _, f := range frames {
		frame := make([]byte, 24)
		binary.BigEndian.PutUint32(frame, f.page)
		binary.BigEndian.PutUint32(frame[4:], f.commit)
		copy(frame[8:], salt)
		state, _ = checksum(frame[:8], state, binary.LittleEndian)
		state, _ = checksum(f.body, state, binary.LittleEndian)
		binary.BigEndian.PutUint32(frame[16:], state[0])
		binary.BigEndian.PutUint32(frame[20:], state[1])
		output = append(append(output, frame...), f.body...)
	}
	return output
}

func TestWALCommitRestartTruncationAndCorruption(t *testing.T) {
	block := func(b byte) []byte { return bytes.Repeat([]byte{b}, 4096) }
	tests := []struct {
		name            string
		base, wal, want []byte
	}{
		{"uncommitted", block(0), journal(t, "12345678", walFrame{1, 0, block(1)}), block(0)},
		{"commit", block(0), journal(t, "12345678", walFrame{1, 1, block(1)}, walFrame{1, 0, block(2)}), block(1)},
		{"truncate", block(0), journal(t, "12345678", walFrame{2, 2, block(2)}, walFrame{1, 1, block(3)}, walFrame{2, 2, block(4)}), append(block(3), block(4)...)},
		{"restart", block(0), append(journal(t, "12345678", walFrame{1, 1, block(1)}), journal(t, "87654321", walFrame{1, 1, block(2)})[32:]...), block(1)},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			got, err := applyWAL(test.base, test.wal, nil, 4096, false)
			must(t, err)
			if !bytes.Equal(got, test.want) {
				t.Fatal("wrong committed snapshot")
			}
		})
	}
	bad := journal(t, "12345678", walFrame{1, 1, block(1)})
	bad[len(bad)-1] ^= 1
	if _, err := applyWAL(block(0), bad, nil, 4096, false); err == nil {
		t.Fatal("corrupt WAL accepted")
	}
}

func TestEncryptedSnapshotAndReadonlyIntegrity(t *testing.T) {
	root := t.TempDir()
	source := filepath.Join(root, "source")
	datasets(t, source)
	raw, err := os.ReadFile(filepath.Join(source, "message.db"))
	must(t, err)
	got, err := applyWAL(raw[:4096], journal(t, "12345678", walFrame{1, 1, encryptPage(t, raw[:4096], 1)}), testKey, 4096, true)
	must(t, err)
	if !bytes.Equal(got, raw[:4096]) {
		t.Fatal("encrypted WAL mismatch")
	}
	encryptDataset(t, source)
	keys := map[string]string{}
	for _, name := range dbNames {
		keys[name] = strings.Repeat("ff", 16)
	}
	if _, err := snapshot(source, keys, filepath.Join(root, "bad")); err == nil {
		t.Fatal("wrong key accepted")
	}
	if _, err := os.Stat(filepath.Join(root, "bad")); !os.IsNotExist(err) {
		t.Fatal("failed snapshot retained")
	}
	for _, name := range dbNames {
		keys[name] = hex.EncodeToString(testKey)
	}
	before, _ := fingerprints(source)
	_, err = snapshot(source, keys, filepath.Join(root, "good"))
	must(t, err)
	after, _ := fingerprints(source)
	if !reflect.DeepEqual(before, after) {
		t.Fatal("source files changed")
	}
	db, err := readonly(filepath.Join(root, "good/message.db"))
	must(t, err)
	defer db.Close()
	if _, err := db.Exec("DELETE FROM message_table"); err == nil {
		t.Fatal("snapshot connection permits writes")
	}
	context, err := buildContext(filepath.Join(root, "good"), "")
	must(t, err)
	if context["selected"].(object)["id"] != "S1" || !strings.Contains(str(context["summary"]), "请明天") {
		t.Fatal(context)
	}
}

func TestRealSQLiteWALSourceUnchanged(t *testing.T) {
	root := t.TempDir()
	source := filepath.Join(root, "source")
	datasets(t, source)
	db, err := sql.Open("sqlite", filepath.Join(source, "message.db"))
	must(t, err)
	defer db.Close()
	_, err = db.Exec(`PRAGMA journal_mode=WAL; PRAGMA wal_checkpoint(TRUNCATE);
INSERT INTO message_table VALUES('S1','20',1795200001000,'WAL里的已提交原文','U1',NULL);`)
	must(t, err)
	before := map[string][]byte{}
	entries, err := os.ReadDir(source)
	must(t, err)
	for _, entry := range entries {
		before[entry.Name()], err = os.ReadFile(filepath.Join(source, entry.Name()))
		must(t, err)
	}
	sig, err := fingerprints(source)
	must(t, err)
	_, err = snapshot(source, nil, filepath.Join(root, "copy"))
	must(t, err)
	after, err := fingerprints(source)
	must(t, err)
	if !reflect.DeepEqual(sig, after) {
		t.Fatal("source fingerprint changed")
	}
	for name, raw := range before {
		current, err := os.ReadFile(filepath.Join(source, name))
		must(t, err)
		if !bytes.Equal(raw, current) {
			t.Fatal("source DB/WAL/SHM changed:", name)
		}
	}
	view := &Engine{current: filepath.Join(root, "copy"), source: source}
	page, err := view.Query("/api/messages", object{"chat": "S1"})
	must(t, err)
	if len(page["messages"].([]object)) != 2 {
		t.Fatal("committed WAL message not merged")
	}
}

func TestKeyRestoreAccountSwitchAndRuntimeGuard(t *testing.T) {
	var events bytes.Buffer
	root := t.TempDir()
	source := filepath.Join(root, "source")
	datasets(t, source)
	encryptDataset(t, source)
	e, err := New(Options{Root: filepath.Join(root, "state")}, &events)
	must(t, err)
	defer e.Close()
	e.processes = func() []int { return []int{123} }
	e.discover = func([]int) (string, error) { return source, nil }
	e.clientChecked = 123
	must(t, e.sync())
	if e.pendingAccount == "" || e.attemptedPID != 0 || e.current != "" {
		t.Fatal("attach attempted before key restoration")
	}
	keys := object{}
	for _, name := range dbNames {
		keys[name] = hex.EncodeToString(testKey)
	}
	e.Action(object{"op": "keys", "account": "wrong-account", "keys": keys})
	if e.pendingAccount == "" {
		t.Fatal("wrong account completed key restoration")
	}
	e.Action(object{"op": "keys", "account": e.account(), "keys": keys})
	must(t, e.sync())
	if e.current == "" || e.attemptedPID != 0 {
		t.Fatal("restored keys did not connect without attach")
	}
	if _, err := e.Query("/api/messages", object{"chat": "S1", "_account": "wrong"}); err == nil {
		t.Fatal("account guard missing")
	}
	e.startRuntime()
	if e.managedPID != 0 || !strings.Contains(events.String(), "尚未正常退出") {
		t.Fatal("official client running did not prevent spawn")
	}
	e.processes = func() []int { return []int{123, 456} }
	must(t, e.sync())
	if e.account() != "" || e.current != "" {
		t.Fatal("ambiguous processes retained stale data")
	}
}

func TestDiscoveryIgnoresHistoricalAccounts(t *testing.T) {
	container := t.TempDir()
	data := filepath.Join(container, "Library/Application Support/WXWork/Data")
	datasets(t, filepath.Join(data, "111"))
	datasets(t, filepath.Join(data, "222"))
	source, err := discover([]int{1}, container, func(int) []string { return []string{filepath.Join(data, "111/Avator/a.db")} })
	must(t, err)
	if source != filepath.Join(data, "111") {
		t.Fatal(source)
	}
	source, err = discover([]int{1}, container, func(int) []string { return nil })
	must(t, err)
	if source != "" {
		t.Fatal("historical account selected without login evidence")
	}
}

func TestContentPreservesTextAndRejectsUnknown(t *testing.T) {
	body := append([]byte{10, 12, 8, 0, 18, 8, 10, 6}, []byte("你好")...)
	for _, test := range []struct {
		value, kind any
		want        string
	}{
		{body, int64(2), "你好"},
		{body[:len(body)-1], int64(2), "[暂未解析的结构化消息]"},
		{[]byte{255, 1}, nil, "[暂未解析的二进制消息]"},
		{`{"text":"锁定原文"}`, nil, "锁定原文"},
		{"x\x01y", nil, "[暂未解析的结构化消息]"},
	} {
		if got := readableContent(test.value, test.kind); got != test.want {
			t.Fatalf("%q != %q", got, test.want)
		}
	}
}

func TestIndependentPythonCipherVector(t *testing.T) {
	// Produced by the former Python AES/MD5 implementation, not this Go cipher.
	raw, err := hex.DecodeString("84eecd2c190aaea395da6b9b11fc3b105df7c6717f9d65152de271f35f47355a")
	must(t, err)
	got, err := decodePage(raw, testKey, 42)
	must(t, err)
	want := make([]byte, 32)
	for i := range want {
		want[i] = byte(i)
	}
	if !bytes.Equal(got, want) {
		t.Fatal("page key/IV differ from the independently generated vector")
	}
}

func TestPendingQueryKeepsQuitResponsive(t *testing.T) {
	e, err := New(Options{Root: t.TempDir()}, io.Discard)
	must(t, err)
	e.mu.Lock()
	e.Respond(object{"op": "query", "request_id": "pending", "path": "/api/status"})
	e.Handle(object{"op": "keys", "account": "pending", "keys": object{}})
	e.Action(object{"op": "quit"})
	select {
	case <-e.ctx.Done():
	default:
		t.Fatal("queued query blocked quit")
	}
	e.mu.Unlock()
	e.Close()
}

func TestMain(m *testing.M) {
	if os.Getenv("WECOM_CAPTURE_FIXTURE") == "1" {
		encoder := json.NewEncoder(os.Stdout)
		encoder.Encode(object{"type": "candidate", "value": hex.EncodeToString(testKey)})
		encoder.Encode(object{"type": "installed", "count": 1})
		io.Copy(io.Discard, os.Stdin)
		os.Exit(0)
	}
	os.Exit(m.Run())
}

func TestCapturePipeReadyBeforeCandidateHandler(t *testing.T) {
	t.Setenv("WECOM_CAPTURE_FIXTURE", "1")
	root := t.TempDir()
	executable, err := os.Executable()
	must(t, err)
	arch := runtime.GOARCH
	if arch == "amd64" {
		arch = "x86_64"
	}
	path := filepath.Join(root, "wechat-realtime-reader/skills/wechat-db-keys/vendor/frida/frida-inject-"+arch)
	must(t, os.MkdirAll(filepath.Dir(path), 0700))
	must(t, os.Symlink(executable, path))
	e, err := New(Options{Root: filepath.Join(root, "state"), PlatformRoot: root}, io.Discard)
	must(t, err)
	e.mu.Lock()
	err = e.attach(123)
	e.mu.Unlock()
	must(t, err)
	e.Close()
	// Race detector also checks capture event delivery vs shutdown.
	if len(e.candidates) != 1 || e.candidates[0] != hex.EncodeToString(testKey) {
		t.Fatal("candidate pipe not consumed")
	}
}
