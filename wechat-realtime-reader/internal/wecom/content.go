package wecom

import (
	"encoding/binary"
	"encoding/json"
	"fmt"
	"unicode/utf8"
)

type protoField struct {
	number, wire uint64
	value        uint64
	bytes        []byte
}

func protobufFields(blob []byte) ([]protoField, error) {
	if len(blob) > 2<<20 {
		return nil, fmt.Errorf("消息结构过大")
	}
	fields := []protoField{}
	for len(blob) > 0 {
		tag, n := binary.Uvarint(blob)
		if n <= 0 || tag>>3 == 0 || len(fields) >= 2048 {
			return nil, fmt.Errorf("消息结构无效")
		}
		blob = blob[n:]
		field := protoField{number: tag >> 3, wire: tag & 7}
		if field.wire == 0 {
			field.value, n = binary.Uvarint(blob)
			if n <= 0 {
				return nil, fmt.Errorf("消息结构不完整")
			}
			blob = blob[n:]
		} else {
			size := uint64(0)
			switch field.wire {
			case 1:
				size = 8
			case 2:
				size, n = binary.Uvarint(blob)
				if n <= 0 {
					return nil, fmt.Errorf("消息结构不完整")
				}
				blob = blob[n:]
			case 5:
				size = 4
			default:
				return nil, fmt.Errorf("消息结构尚未支持")
			}
			if size > uint64(len(blob)) {
				return nil, fmt.Errorf("消息结构不完整")
			}
			field.bytes, blob = blob[:int(size)], blob[int(size):]
		}
		fields = append(fields, field)
	}
	return fields, nil
}

func firstField(fields []protoField, number, wire uint64) *protoField {
	for i := range fields {
		if fields[i].number == number && fields[i].wire == wire {
			return &fields[i]
		}
	}
	return nil
}

func controlText(text string) bool {
	for _, c := range text {
		if c < 32 && c != '\t' && c != '\r' && c != '\n' {
			return true
		}
	}
	return false
}

func decodeWeComText(blob []byte) (string, error) {
	runs, err := protobufFields(blob)
	if err != nil || len(runs) == 0 {
		return "", fmt.Errorf("消息结构尚未支持")
	}
	text := ""
	for _, run := range runs {
		if run.number != 1 || run.wire != 2 {
			return "", fmt.Errorf("消息结构尚未支持")
		}
		items, err := protobufFields(run.bytes)
		if err != nil {
			return "", err
		}
		kind, payload := firstField(items, 1, 0), firstField(items, 2, 2)
		if kind == nil || payload == nil || (kind.value != 0 && kind.value != 10) {
			text += "[暂未解析的消息片段]"
			continue
		}
		nested, err := protobufFields(payload.bytes)
		if err != nil {
			return "", err
		}
		field := uint64(1)
		if kind.value == 10 {
			field = 2
		}
		raw := firstField(nested, field, 2)
		if raw == nil || !utf8.Valid(raw.bytes) || controlText(string(raw.bytes)) {
			return "", fmt.Errorf("文本片段无效")
		}
		text += string(raw.bytes)
	}
	return text, nil
}

func readableContent(value, kind any) string {
	if value == nil {
		return ""
	}
	if blob, ok := value.([]byte); ok {
		if kind != nil && (number(kind) == 0 || number(kind) == 2) {
			if text, err := decodeWeComText(blob); err == nil {
				return text
			}
			return "[暂未解析的结构化消息]"
		}
		if !utf8.Valid(blob) {
			return "[暂未解析的二进制消息]"
		}
		value = string(blob)
	}
	text := str(value)
	var parsed map[string]any
	if json.Unmarshal([]byte(text), &parsed) == nil {
		for _, key := range []string{"text", "content", "title", "msg"} {
			if s, ok := parsed[key].(string); ok {
				text = s
				break
			}
		}
	}
	if controlText(text) {
		return "[暂未解析的结构化消息]"
	}
	return text
}
