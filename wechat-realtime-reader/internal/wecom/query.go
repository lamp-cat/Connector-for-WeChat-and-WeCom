package wecom

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"golang.org/x/text/cases"
)

type object = map[string]any

var capabilities = object{
	"sessions": true, "contacts": true, "messages": true, "search": true,
	"pagination": true, "members": false, "media": false, "is_self": false,
	"conversation_kind": false,
}

type queryError struct {
	status int
	text   string
}

func (e *queryError) Error() string { return e.text }
func unsupported(text string) error { return &queryError{501, text} }
func failure(err error) (int, string) {
	var qe *queryError
	if errors.As(err, &qe) {
		return qe.status, qe.text
	}
	// Database/OS errors may contain private paths or data.
	return 503, "快照查询未完成"
}
func invalid(text string) error {
	code := 503
	if strings.Contains(text, "账号") {
		code = 409
	}
	return &queryError{code, text}
}

func str(v any) string {
	if v == nil {
		return ""
	}
	if s, ok := v.(string); ok {
		return s
	}
	if b, ok := v.([]byte); ok {
		return string(b)
	}
	return fmt.Sprint(v)
}

func number(v any) float64 {
	n, _ := strconv.ParseFloat(str(v), 64)
	return n
}

func columns(db *sql.DB, table string) (map[string]bool, error) {
	rows, err := db.Query(`PRAGMA table_info("` + table + `")`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	cols := map[string]bool{}
	for rows.Next() {
		var index, notnull, pk int
		var name, typ string
		var def any
		if err := rows.Scan(&index, &name, &typ, &notnull, &def, &pk); err != nil {
			return nil, err
		}
		cols[name] = true
	}
	return cols, rows.Err()
}

func tableRows(db *sql.DB, table string) ([]object, error) {
	rows, err := db.Query(`SELECT * FROM "` + table + `"`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	names, err := rows.Columns()
	if err != nil {
		return nil, err
	}
	result := []object{}
	for rows.Next() {
		values := make([]any, len(names))
		ptrs := make([]any, len(names))
		for i := range values {
			ptrs[i] = &values[i]
		}
		if err := rows.Scan(ptrs...); err != nil {
			return nil, err
		}
		row := object{}
		for i, name := range names {
			row[name] = values[i]
		}
		result = append(result, row)
	}
	return result, rows.Err()
}

func userNames(folder string) (map[string]string, error) {
	db, err := readonly(filepath.Join(folder, "user.db"))
	if err != nil {
		return nil, err
	}
	defer db.Close()
	cols, err := columns(db, "user_table")
	if err != nil {
		return nil, err
	}
	result := map[string]string{}
	if !cols["id"] || !cols["name"] {
		return result, nil
	}
	rows, err := tableRows(db, "user_table")
	if err != nil {
		return nil, err
	}
	for _, row := range rows {
		name := str(row["real_name"])
		if name == "" {
			name = str(row["name"])
		}
		if name == "" {
			name = str(row["id"])
		}
		result[str(row["id"])] = name
	}
	return result, nil
}

func messageSegments(db *sql.DB) (string, error) {
	segments := []string{}
	for _, table := range []string{"message_table", "message_small_table", "kf_message_tableV1"} {
		cols, err := columns(db, table)
		if err != nil {
			return "", err
		}
		if !cols["conversation_id"] || !cols["message_id"] || !cols["send_time"] || !cols["content"] {
			continue
		}
		sender, kind := "''", "NULL"
		if cols["sender_id"] {
			sender = "CAST(sender_id AS TEXT)"
		}
		if cols["content_type"] {
			kind = "content_type"
		}
		segments = append(segments, fmt.Sprintf(`SELECT CAST(conversation_id AS TEXT) cid,
			CAST(message_id AS TEXT) mid, %s sender,
			CASE WHEN send_time>100000000000 THEN send_time/1000.0 ELSE send_time END ts,
			content, %s kind, '%s' src FROM "%s"`, sender, kind, table, table))
	}
	if len(segments) == 0 {
		return "", invalid("此版本企业微信的消息结构尚未支持")
	}
	return strings.Join(segments, " UNION ALL "), nil
}

type messageRow struct {
	cid, mid, sender, src string
	ts                    float64
	content, kind         any
	text                  string
}

func scanMessage(rows *sql.Rows) (messageRow, error) {
	var r messageRow
	var cid, mid, sender, ts any
	err := rows.Scan(&cid, &mid, &sender, &ts, &r.content, &r.kind, &r.src)
	r.cid, r.mid, r.sender, r.ts = str(cid), str(mid), str(sender), number(ts)
	r.text = readableContent(r.content, r.kind)
	return r, err
}

func sessions(folder string) ([]object, error) {
	db, err := readonly(filepath.Join(folder, "session.db"))
	if err != nil {
		return nil, err
	}
	defer db.Close()
	cols, err := columns(db, "conversation_table")
	if err != nil {
		return nil, err
	}
	if !cols["id"] {
		return nil, invalid("此版本企业微信的会话结构尚未支持")
	}
	rows, err := tableRows(db, "conversation_table")
	if err != nil {
		return nil, err
	}
	result := []object{}
	for _, r := range rows {
		ts := number(r["last_message_time"])
		if ts > 1e11 {
			ts /= 1000
		}
		name := str(r["roomname_remark"])
		if name == "" {
			name = str(r["name"])
		}
		if name == "" {
			name = str(r["id"])
		}
		result = append(result, object{"id": str(r["id"]), "name": name, "ts": ts})
	}
	if len(result) == 0 {
		return result, nil
	}
	messages, err := readonly(filepath.Join(folder, "message.db"))
	if err != nil {
		return nil, err
	}
	defer messages.Close()
	segment, err := messageSegments(messages)
	if err != nil {
		return nil, err
	}
	latest, err := messages.Query("SELECT cid,MAX(ts) FROM (" + segment + ") GROUP BY cid")
	if err != nil {
		return nil, err
	}
	defer latest.Close()
	activity := map[string]float64{}
	for latest.Next() {
		var cid, ts any
		if err := latest.Scan(&cid, &ts); err != nil {
			return nil, err
		}
		activity[str(cid)] = number(ts)
	}
	if err := latest.Err(); err != nil {
		return nil, err
	}
	for _, s := range result {
		s["ts"] = math.Max(number(s["ts"]), activity[str(s["id"])])
	}
	sort.SliceStable(result, func(i, j int) bool {
		if result[i]["ts"] == result[j]["ts"] {
			return str(result[i]["id"]) > str(result[j]["id"])
		}
		return number(result[i]["ts"]) > number(result[j]["ts"])
	})
	return result, nil
}

func cursor(row messageRow, account, chat string) string {
	raw, _ := json.Marshal([]any{account, chat, row.ts, row.mid, row.src})
	return base64.RawURLEncoding.EncodeToString(raw)
}

func parseCursor(value any, account, chat string) ([]any, error) {
	raw, err := base64.RawURLEncoding.DecodeString(strings.TrimRight(str(value), "="))
	var parts []any
	if err != nil || json.Unmarshal(raw, &parts) != nil || len(parts) != 5 ||
		parts[0] != account || parts[1] != chat {
		return nil, invalid("分页游标无效，或不属于当前账号和会话")
	}
	ts, ok := parts[2].(float64)
	_, midOK := parts[3].(string)
	_, srcOK := parts[4].(string)
	if !ok || !midOK || !srcOK || math.IsNaN(ts) || math.IsInf(ts, 0) {
		return nil, invalid("分页游标无效，或不属于当前账号和会话")
	}
	return parts[2:], nil
}

func timestamp(v any) (float64, error) {
	s := str(v)
	if strings.HasSuffix(s, "d") {
		if days, err := strconv.ParseUint(strings.TrimSuffix(s, "d"), 10, 32); err == nil {
			return float64(time.Now().UnixNano())/1e9 - float64(days)*86400, nil
		}
	}
	if n, err := strconv.ParseFloat(s, 64); err == nil && !math.IsNaN(n) && !math.IsInf(n, 0) {
		return n, nil
	}
	for _, format := range []string{time.RFC3339Nano, "2006-01-02T15:04:05.999999999", "2006-01-02 15:04:05.999999999", "2006-01-02"} {
		if t, err := time.ParseInLocation(format, s, time.Local); err == nil {
			return float64(t.Unix()) + float64(t.Nanosecond())/1e9, nil
		}
	}
	return 0, invalid("时间范围无效")
}

func isoTime(ts float64) string {
	sec, fraction := math.Modf(ts)
	t := time.Unix(int64(sec), int64(math.Round(fraction*1e6))*1000).In(time.Local)
	if t.Nanosecond() == 0 {
		return t.Format("2006-01-02T15:04:05-07:00")
	}
	return t.Format("2006-01-02T15:04:05.000000-07:00")
}

func boundedInt(params object, key string, fallback, max int) (int, error) {
	s := str(params[key])
	if s == "" || s == "0" {
		return fallback, nil
	}
	n, err := strconv.Atoi(s)
	if err != nil {
		return 0, invalid("查询数量无效")
	}
	if n < 1 {
		n = 1
	}
	if n > max {
		n = max
	}
	return n, nil
}

// Query holds the engine lock so an account switch cannot replace a snapshot mid-read.
func (e *Engine) Query(path string, params object) (object, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	account := e.account()
	if path == "/api/status" {
		state := "unavailable"
		if e.current != "" {
			state = "fresh"
		}
		return object{"source": "wecom", "account_id": nullable(account), "ready": e.current != "",
			"capabilities": capabilities, "freshness": object{"state": state},
			"updated": e.updated, "receive_only": true}, nil
	}
	if e.current == "" || account == "" {
		return nil, invalid("企业微信尚未生成已验证的快照，请先完成连接")
	}
	if expected := str(params["_account"]); expected != "" && expected != account {
		return nil, invalid("账号已变化，已取消本次读取")
	}
	limit, err := boundedInt(params, "limit", 60, 1000)
	if err != nil {
		return nil, err
	}
	base := object{"source": "wecom", "account_id": account, "snapshot_revision": e.signature,
		"freshness": object{"state": "fresh"}}
	fold := cases.Fold()
	keyword := fold.String(str(params["keyword"]))
	if path == "/api/sessions" || path == "/api/active-chat" {
		raw, err := sessions(e.current)
		if err != nil {
			return nil, err
		}
		list := []object{}
		for _, s := range raw {
			if strings.Contains(fold.String(str(s["name"])+str(s["id"])), keyword) &&
				(str(params["kind"]) == "" || str(params["kind"]) == "unknown") {
				list = append(list, object{"username": s["id"], "name": s["name"], "kind": "unknown",
					"last_time": isoTime(number(s["ts"])), "unread": nil})
			}
		}
		if path == "/api/active-chat" {
			var active any
			for _, s := range list {
				if s["username"] == e.chosen {
					active = s
				}
			}
			if active == nil && len(list) > 0 {
				active = list[0]
			}
			top, err := boundedInt(params, "top", 5, 1000)
			if err != nil {
				return nil, err
			}
			base["active"], base["candidates"], base["estimate"] = active, list[:min(top, len(list))], true
			base["signal"] = "wecom.selected_or_recent"
			base["note"] = "企业微信：所选会话或最近活跃会话，不代表当前前台窗口。"
		} else {
			base["sessions"], base["total"] = list[:min(limit, len(list))], len(list)
		}
		return base, nil
	}
	names, err := userNames(e.current)
	if err != nil {
		return nil, err
	}
	if path == "/api/contacts" {
		list := []object{}
		ids := make([]string, 0, len(names))
		for id := range names {
			ids = append(ids, id)
		}
		sort.Strings(ids)
		for _, id := range ids {
			if strings.Contains(fold.String(id+names[id]), keyword) {
				list = append(list, object{"username": id, "name": names[id], "kind": "unknown"})
			}
		}
		base["contacts"] = list[:min(limit, len(list))]
		return base, nil
	}
	if path != "/api/messages" && path != "/api/search" {
		return nil, unsupported("企业微信暂不支持此能力（成员或媒体读取）")
	}
	chat := str(params["chat"])
	if path == "/api/messages" && chat == "" {
		return nil, invalid("chat is required")
	}
	conditions, args := []string{}, []any{}
	for _, pair := range [][2]string{{"chat", "cid"}, {"sender", "sender"}} {
		if v := str(params[pair[0]]); v != "" {
			conditions, args = append(conditions, pair[1]+"=?"), append(args, v)
		}
	}
	for _, pair := range [][2]string{{"before", "<"}, {"after", ">"}} {
		if str(params[pair[0]]) != "" {
			bound, err := parseCursor(params[pair[0]], account, chat)
			if err != nil {
				return nil, err
			}
			conditions = append(conditions, "(ts,mid,src) "+pair[1]+" (?,?,?)")
			args = append(args, bound...)
		}
	}
	for _, pair := range [][2]string{{"since", ">="}, {"until", "<="}} {
		if str(params[pair[0]]) != "" {
			bound, err := timestamp(params[pair[0]])
			if err != nil {
				return nil, err
			}
			conditions, args = append(conditions, "ts "+pair[1]+" ?"), append(args, bound)
		}
	}
	if str(params["types"]) != "" {
		return nil, unsupported("企业微信原始类型码尚未映射，暂不支持 types 筛选")
	}
	db, err := readonly(filepath.Join(e.current, "message.db"))
	if err != nil {
		return nil, err
	}
	defer db.Close()
	segment, err := messageSegments(db)
	if err != nil {
		return nil, err
	}
	statement := "SELECT * FROM (" + segment + ")"
	if len(conditions) > 0 {
		statement += " WHERE " + strings.Join(conditions, " AND ")
	}
	statement += " ORDER BY ts DESC,mid DESC,src DESC"
	ctx := e.ctx
	if ctx == nil || e.options.Snapshot != "" {
		// Offline fixtures may queue a query immediately followed by EOF/quit.
		ctx = context.Background()
	}
	rows, err := db.QueryContext(ctx, statement, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	results := []messageRow{}
	for rows.Next() {
		r, err := scanMessage(rows)
		if err != nil {
			return nil, err
		}
		if keyword != "" && !strings.Contains(fold.String(r.text), keyword) {
			continue
		}
		results = append(results, r)
		if len(results) > limit {
			break
		}
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	page := results[:min(limit, len(results))]
	messages := []object{}
	for i := len(page) - 1; i >= 0; i-- {
		r := page[i]
		identity := r.src + ":" + r.mid
		hash := sha256.Sum256([]byte(identity))
		kind := "other"
		if r.text != "" && !strings.HasPrefix(r.text, "[") {
			kind = "text"
		}
		name := names[r.sender]
		if name == "" {
			name = r.sender
		}
		messages = append(messages, object{"source": "wecom", "account_id": account, "chat": r.cid,
			"message_id": identity, "server_id": r.mid,
			"local_id": binary.BigEndian.Uint64(hash[:8]) & (1<<63 - 1), "seq": int64(r.ts * 1e6),
			"cursor": cursor(r, account, chat), "sender": r.sender, "sender_name": name,
			"is_self": nil, "kind": kind, "type": r.kind, "time": isoTime(r.ts), "text": r.text,
			"extra": object{"source_table": r.src, "is_self_unknown": true}})
	}
	base["messages"], base["has_more"] = messages, len(results) > limit
	base["next_before"], base["next_after"] = nil, params["after"]
	if len(page) > 0 {
		base["next_before"], base["next_after"] = cursor(page[len(page)-1], account, chat), cursor(page[0], account, chat)
	}
	return base, nil
}

func buildContext(folder, chosen string) (object, error) {
	list, err := sessions(folder)
	if err != nil {
		return nil, err
	}
	context := object{"sessions": list, "selected": nil, "messages": []object{}, "summary": "当前账号暂无会话。", "todos": []string{}}
	if len(list) == 0 {
		return context, nil
	}
	selected := list[0]
	for _, s := range list {
		if s["id"] == chosen {
			selected = s
		}
	}
	// Query reuses only validated snapshot paths, including in this ephemeral view.
	view := &Engine{current: folder, source: folder, accountOverride: "context"}
	page, err := view.Query("/api/messages", object{"chat": selected["id"], "limit": 60})
	if err != nil {
		return nil, err
	}
	messages, texts := []object{}, []object{}
	for _, m := range page["messages"].([]object) {
		ts, _ := timestamp(m["time"])
		row := object{"sender": m["sender_name"], "text": m["text"], "ts": ts}
		messages = append(messages, row)
		if str(m["text"]) != "" && !strings.HasPrefix(str(m["text"]), "[") {
			texts = append(texts, row)
		}
	}
	summary, todos := []string{}, []string{}
	for _, m := range texts[max(0, len(texts)-5):] {
		summary = append(summary, str(m["sender"])+"："+truncate(str(m["text"]), 420))
	}
	for _, m := range texts[max(0, len(texts)-30):] {
		for _, word := range []string{"请", "需要", "麻烦", "确认", "截止", "明天", "今天", "安排", "待办", "提交", "跟进"} {
			if strings.Contains(str(m["text"]), word) {
				todos = append(todos, str(m["sender"])+"："+truncate(str(m["text"]), 280))
				break
			}
		}
	}
	text := strings.Join(summary, "\n\n")
	if text == "" {
		text = "最近消息暂未解析为文本。"
	}
	mode := "recent"
	if selected["id"] == chosen {
		mode = "selected"
	}
	context["selected"], context["messages"], context["summary"] = selected, messages, text
	context["todos"], context["context_mode"] = todos[max(0, len(todos)-6):], mode
	return context, nil
}

func truncate(s string, n int) string {
	runes := []rune(s)
	return string(runes[:min(n, len(runes))])
}

func nullable(s string) any {
	if s == "" {
		return nil
	}
	return s
}
