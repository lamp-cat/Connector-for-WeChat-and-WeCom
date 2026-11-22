// Package wecom reads WeCom snapshots. Its page cipher is distinct from WeChat SQLCipher.
package wecom

import (
	"bytes"
	"crypto/aes"
	"crypto/cipher"
	"crypto/md5"
	"encoding/binary"
	"fmt"
)

var dbNames = []string{"message.db", "session.db", "user.db"}
var magic = []byte("SQLite format 3\x00")

func pageLength(header []byte) (int, error) {
	if len(header) < 24 {
		return 0, fmt.Errorf("数据库头不完整")
	}
	n := int(binary.BigEndian.Uint16(header[16:18]))
	if n == 1 {
		n = 65536
	}
	if n < 512 || n > 65536 || n&(n-1) != 0 {
		return 0, fmt.Errorf("当前数据库格式尚未支持")
	}
	return n, nil
}

func pageCipher(key []byte, page uint32) (cipher.Block, []byte, error) {
	if len(key) != 16 {
		return nil, nil, fmt.Errorf("密钥未通过验证")
	}
	input := make([]byte, 24)
	copy(input, key)
	binary.LittleEndian.PutUint32(input[16:], page)
	copy(input[20:], "sAlT")
	derived := md5.Sum(input)
	seed := int64(page) + 1
	values := make([]byte, 16)
	for i := 0; i < 4; i++ {
		seed = 40692*(seed%52774) - 3791*(seed/52774)
		if seed < 0 {
			seed += 2147483399
		}
		binary.LittleEndian.PutUint32(values[i*4:], uint32(seed))
	}
	iv := md5.Sum(values)
	block, err := aes.NewCipher(derived[:])
	return block, iv[:], err
}

func decodePage(raw, key []byte, page uint32) ([]byte, error) {
	if len(raw) == 0 || len(raw)%16 != 0 {
		return nil, fmt.Errorf("数据库页不完整")
	}
	block, iv, err := pageCipher(key, page)
	if err != nil {
		return nil, err
	}
	if page != 1 {
		plain := make([]byte, len(raw))
		cipher.NewCBCDecrypter(block, iv).CryptBlocks(plain, raw)
		return plain, nil
	}
	if len(raw) < 32 || !bytes.Equal(raw[21:24], []byte{64, 32, 32}) {
		return nil, fmt.Errorf("当前数据库加密格式尚未支持")
	}
	body := append(append([]byte{}, raw[8:16]...), raw[24:]...)
	decoded := make([]byte, len(body))
	cipher.NewCBCDecrypter(block, iv).CryptBlocks(decoded, body)
	if !bytes.Equal(decoded[:8], raw[16:24]) {
		return nil, fmt.Errorf("密钥未通过数据库校验")
	}
	return append(append([]byte{}, magic...), decoded...), nil
}

func checksum(blob []byte, state [2]uint32, order binary.ByteOrder) ([2]uint32, error) {
	if len(blob)%8 != 0 {
		return state, fmt.Errorf("WAL 校验长度错误")
	}
	for i := 0; i < len(blob); i += 8 {
		state[0] += order.Uint32(blob[i:]) + state[1]
		state[1] += order.Uint32(blob[i+4:]) + state[0]
	}
	return state, nil
}

func applyWAL(base, journal, key []byte, size int, encrypted bool) ([]byte, error) {
	if len(journal) == 0 {
		return base, nil
	}
	if len(journal) < 32 {
		return nil, fmt.Errorf("WAL 正在写入，稍后重试")
	}
	tag := binary.BigEndian.Uint32(journal)
	if (tag != 0x377f0682 && tag != 0x377f0683) ||
		binary.BigEndian.Uint32(journal[4:]) != 3007000 || int(binary.BigEndian.Uint32(journal[8:])) != size {
		return nil, fmt.Errorf("WAL 格式尚未支持")
	}
	var order binary.ByteOrder = binary.LittleEndian
	if tag == 0x377f0683 {
		order = binary.BigEndian
	}
	state, _ := checksum(journal[:24], [2]uint32{}, order)
	valid := func(frame []byte) bool {
		return state[0] == binary.BigEndian.Uint32(frame) && state[1] == binary.BigEndian.Uint32(frame[4:])
	}
	if !valid(journal[24:]) {
		return nil, fmt.Errorf("WAL 头部校验失败")
	}
	pending, updates := map[uint32][]byte{}, map[uint32][]byte{}
	total := uint32(0)
	for pos, step := 32, 24+size; pos+step <= len(journal); pos += step {
		frame, body := journal[pos:pos+24], journal[pos+24:pos+step]
		if !bytes.Equal(frame[8:16], journal[16:24]) {
			break // A previous-generation tail is not part of this WAL.
		}
		page, count := binary.BigEndian.Uint32(frame), binary.BigEndian.Uint32(frame[4:])
		if page == 0 {
			return nil, fmt.Errorf("WAL 已轮转，稍后重试")
		}
		state, _ = checksum(frame[:8], state, order)
		state, _ = checksum(body, state, order)
		if !valid(frame[16:]) {
			return nil, fmt.Errorf("WAL 帧校验失败")
		}
		pending[page] = body
		if count != 0 {
			if count > 1_000_000 || uint64(count)*uint64(size) > 2<<30 {
				return nil, fmt.Errorf("数据库规模超出当前读取上限")
			}
			for n, b := range pending {
				updates[n] = b
			}
			for n := range updates {
				if n > count {
					delete(updates, n)
				}
			}
			pending = map[uint32][]byte{}
			total = count
		}
	}
	if total == 0 {
		return base, nil
	}
	output := make([]byte, int(total)*size)
	copy(output, base)
	for n, b := range updates {
		var err error
		if encrypted {
			b, err = decodePage(b, key, n)
			if err != nil {
				return nil, err
			}
		}
		copy(output[(int(n)-1)*size:], b)
	}
	return output, nil
}
