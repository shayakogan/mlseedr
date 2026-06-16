// Command mlshaya_segments extracts marketing segments from the Seedr
// ClickHouse warehouse into per-segment CSV files (one row per user_id).
//
// Prerequisites:
//   - the SSH tunnel to data.seedr.cc is up (see SEEDR_DATA_GUIDE.md §2.1);
//   - credentials in ~/.clickhouse.seedr (chmod 600, key=value lines).
//
// Usage:
//
//	go run . -list                 # show the segment catalogue
//	go run .                       # extract every segment to ./segments_out/<date>/
//	go run . -only winback-active  # extract selected segments (comma-separated)
//	go run . -dry                  # print the SQL without running anything
//
// Queries run strictly one at a time — the warehouse is shared (guide §11).
package main

import (
	"bufio"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
)

type config struct {
	user     string
	password string
	host     string
	httpPort string
}

func loadConfig(path string) (*config, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()

	cfg := &config{host: "127.0.0.1", httpPort: "8123"}
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		key, val, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		switch strings.TrimSpace(key) {
		case "user":
			cfg.user = strings.TrimSpace(val)
		case "password":
			cfg.password = strings.TrimSpace(val)
		case "host":
			cfg.host = strings.TrimSpace(val)
		case "http_port":
			cfg.httpPort = strings.TrimSpace(val)
		}
	}
	if err := sc.Err(); err != nil {
		return nil, err
	}
	if cfg.user == "" || cfg.password == "" {
		return nil, fmt.Errorf("%s: user/password missing", path)
	}
	return cfg, nil
}

func (c *config) baseURL() string {
	// The warehouse account is readonly=1, which also forbids overriding
	// settings (max_execution_time etc.) per request — so no URL settings
	// here; the 6-minute client timeout is the only guard rail.
	return fmt.Sprintf("http://%s:%s/", c.host, c.httpPort)
}

// query POSTs sql to ClickHouse and streams the response body to w.
// Returns the number of body lines written (CSV rows incl. header).
func (c *config) query(client *http.Client, sql string, w io.Writer) (int, error) {
	req, err := http.NewRequest(http.MethodPost, c.baseURL(), strings.NewReader(sql))
	if err != nil {
		return 0, err
	}
	req.SetBasicAuth(c.user, c.password)

	resp, err := client.Do(req)
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		msg, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return 0, fmt.Errorf("clickhouse HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(msg)))
	}

	lines := 0
	buf := make([]byte, 256*1024)
	for {
		n, rerr := resp.Body.Read(buf)
		if n > 0 {
			for _, b := range buf[:n] {
				if b == '\n' {
					lines++
				}
			}
			if _, werr := w.Write(buf[:n]); werr != nil {
				return lines, werr
			}
		}
		if rerr == io.EOF {
			return lines, nil
		}
		if rerr != nil {
			return lines, rerr
		}
	}
}

func (c *config) ping(client *http.Client) error {
	var sb strings.Builder
	_, err := c.query(client, "SELECT 1", &sb)
	return err
}

func selectSegments(only string) ([]Segment, error) {
	if only == "" {
		return segments, nil
	}
	byName := map[string]Segment{}
	for _, s := range segments {
		byName[s.Name] = s
	}
	var out []Segment
	for name := range strings.SplitSeq(only, ",") {
		name = strings.TrimSpace(name)
		s, ok := byName[name]
		if !ok {
			return nil, fmt.Errorf("unknown segment %q (use -list)", name)
		}
		out = append(out, s)
	}
	return out, nil
}

func main() {
	var (
		list    = flag.Bool("list", false, "list available segments and exit")
		dry     = flag.Bool("dry", false, "print SQL instead of executing")
		only    = flag.String("only", "", "comma-separated segment names (default: all)")
		outDir  = flag.String("out", "", "output directory (default ./segments_out/<date>)")
		cfgPath = flag.String("config", filepath.Join(os.Getenv("HOME"), ".clickhouse.seedr"), "credentials file")
	)
	flag.Parse()

	if *list {
		for _, s := range segments {
			fmt.Printf("%-26s %s\n%-26s   why: %s\n", s.Name, s.Title, "", s.Why)
		}
		return
	}

	selected, err := selectSegments(*only)
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}

	if *dry {
		for _, s := range selected {
			fmt.Printf("-- %s: %s\n%s\nFORMAT CSVWithNames;\n\n", s.Name, s.Title, strings.TrimSpace(s.SQL))
		}
		return
	}

	cfg, err := loadConfig(*cfgPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, "error loading credentials:", err)
		os.Exit(1)
	}

	client := &http.Client{Timeout: 6 * time.Minute}
	if err := cfg.ping(client); err != nil {
		fmt.Fprintln(os.Stderr, "ClickHouse unreachable (is the SSH tunnel up?):", err)
		os.Exit(1)
	}

	dir := *outDir
	if dir == "" {
		dir = filepath.Join("segments_out", time.Now().UTC().Format("2006-01-02"))
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}

	fmt.Printf("extracting %d segment(s) → %s\n\n", len(selected), dir)
	failed := 0
	for _, s := range selected {
		path := filepath.Join(dir, s.Name+".csv")
		start := time.Now()

		f, err := os.Create(path)
		if err != nil {
			fmt.Fprintln(os.Stderr, "error:", err)
			os.Exit(1)
		}
		lines, qerr := cfg.query(client, strings.TrimSpace(s.SQL)+"\nFORMAT CSVWithNames", f)
		cerr := f.Close()

		switch {
		case qerr != nil:
			failed++
			os.Remove(path)
			fmt.Printf("FAIL  %-26s %v\n", s.Name, qerr)
		case cerr != nil:
			failed++
			fmt.Printf("FAIL  %-26s %v\n", s.Name, cerr)
		default:
			rows := max(lines-1, 0) // minus CSV header
			fmt.Printf("ok    %-26s %7d users  %6.1fs  %s\n", s.Name, rows, time.Since(start).Seconds(), path)
		}
	}
	if failed > 0 {
		fmt.Printf("\n%d segment(s) failed\n", failed)
		os.Exit(1)
	}
}
