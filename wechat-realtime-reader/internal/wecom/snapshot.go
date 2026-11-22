package wecom

import (
	"bytes"
	"database/sql"
	"encoding/hex"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"reflect"
	"syscall"

	_ "modernc.org/sqlite"
)

// Fingerprints retain the former [inode,size,mtime_ns] pipe contract.
type signature []*[3]int64

func fingerprints(source string) (signature, error) {
	result := signature{}
	paths := append(append([]string{}, dbNames...), "message.db-wal", "session.db-wal", "user.db-wal")
	for _, name := range paths {
		st, err := os.Stat(filepath.Join(source, name))
		if os.IsNotExist(err) {
			result = append(result, nil)
			continue
		}
		if err != nil {
			return nil, err
		}
		ino := int64(0)
		if stat, ok := st.Sys().(*syscall.Stat_t); ok {
			ino = int64(stat.Ino)
		}
		result = append(result, &[3]int64{ino, st.Size(), st.ModTime().UnixNano()})
	}
	return result, nil
}

func readonly(path string) (*sql.DB, error) {
	u := url.URL{Scheme: "file", Path: path, RawQuery: "mode=ro&immutable=1"}
	db, err := sql.Open("sqlite", u.String())
	if err == nil {
		db.SetMaxOpenConns(1)
		err = db.Ping()
	}
	if err != nil && db != nil {
		db.Close()
	}
	return db, err
}

func validateDB(path string) error {
	db, err := readonly(path)
	if err != nil {
		return fmt.Errorf("解密后数据库完整性校验失败")
	}
	defer db.Close()
	rows, err := db.Query("PRAGMA quick_check")
	if err != nil {
		return fmt.Errorf("解密后数据库完整性校验失败")
	}
	defer rows.Close()
	count := 0
	for rows.Next() {
		var result string
		if rows.Scan(&result) != nil || result != "ok" {
			return fmt.Errorf("解密后数据库完整性校验失败")
		}
		count++
	}
	if rows.Err() != nil || count != 1 {
		return fmt.Errorf("解密后数据库完整性校验失败")
	}
	return nil
}

func snapshot(source string, keys map[string]string, destination string) (sig signature, err error) {
	paths := append(append([]string{}, dbNames...), "message.db-wal", "session.db-wal", "user.db-wal")
	data := map[string][]byte{}
	stable := false
	for attempt := 0; attempt < 3; attempt++ {
		sig, err = fingerprints(source)
		if err != nil {
			return nil, err
		}
		for _, s := range sig[:3] {
			if s == nil {
				return nil, fmt.Errorf("当前账号数据库尚未就绪")
			}
		}
		missing := false
		for i, name := range paths {
			data[name] = nil
			if sig[i] == nil {
				continue
			}
			if sig[i][1] > 2<<30 {
				return nil, fmt.Errorf("单库超过 2 GB，当前版本暂不支持")
			}
			data[name], err = os.ReadFile(filepath.Join(source, name))
			if os.IsNotExist(err) {
				missing = true
				break
			}
			if err != nil {
				return nil, err
			}
		}
		after, statErr := fingerprints(source)
		if statErr != nil {
			return nil, statErr
		}
		if !missing && reflect.DeepEqual(sig, after) {
			stable = true
			break
		}
	}
	if !stable {
		return nil, fmt.Errorf("企业微信正在更新数据库，稍后重试")
	}
	// Never remove an existing destination, even on failure.
	if err = os.Mkdir(destination, 0700); err != nil {
		return nil, err
	}
	defer func() {
		if err != nil {
			os.RemoveAll(destination)
		}
	}()
	for _, name := range dbNames {
		raw := data[name]
		size, sizeErr := pageLength(raw)
		if sizeErr != nil {
			return nil, sizeErr
		}
		if len(raw)%size != 0 {
			return nil, fmt.Errorf("数据库快照不完整")
		}
		encrypted := !bytes.HasPrefix(raw, magic)
		plain := raw
		key := []byte(nil)
		if encrypted {
			key, err = hex.DecodeString(keys[name])
			if err != nil {
				return nil, fmt.Errorf("密钥未通过验证")
			}
			plain = make([]byte, len(raw))
			for offset := 0; offset < len(raw); offset += size {
				page, decodeErr := decodePage(raw[offset:offset+size], key, uint32(offset/size+1))
				if decodeErr != nil {
					return nil, decodeErr
				}
				copy(plain[offset:], page)
			}
		}
		plain, err = applyWAL(plain, data[name+"-wal"], key, size, encrypted)
		if err != nil {
			return nil, err
		}
		output := filepath.Join(destination, name)
		if err = os.WriteFile(output, plain, 0600); err != nil {
			return nil, err
		}
		if err = validateDB(output); err != nil {
			return nil, err
		}
	}
	return sig, nil
}
