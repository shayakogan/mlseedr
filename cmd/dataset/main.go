// Command dataset builds an ML training set "conversion after a marketing
// email" from the Seedr ClickHouse warehouse.
//
// Sample unit: (user_id, day) on which the user received at least one
// marketing email — every mautic send (mautic WAS the campaign platform,
// 2025-01-10..2026-05-28) and, in the internal_events era (since 2026-05-28),
// sends belonging to bulk campaigns (email_id with >= 1000 sends on that day).
//
// Label: completed transaction in revenue_facts within -label-days (default
// 14) after the send day. label_conv_14d additionally requires the user NOT
// to look premium at send time (free->paid / win-back conversion).
//
// All features are computed strictly BEFORE the send day (windows end at
// d-1), so there is no target leakage. Data-quality rules from
// SEEDR_DATA_GUIDE.md are honored: surface IN ('web','landing') across the
// 2026-05-24..27 migration, goals counted by matomo_idgoal, premium proxied
// from revenue cadence (user_subscription_state is churn-blind and has no
// history).
//
// The warehouse account is read-only, so the build is local: phase "extract"
// streams per-user-day aggregates month by month into a cache directory
// (resumable — existing files are skipped), phase "build" assembles the
// training CSV with rolling windows.
//
// Usage:
//
//	go run ./cmd/dataset -phase extract   # pull caches over the SSH tunnel
//	go run ./cmd/dataset -phase build     # assemble train CSV from caches
//	go run ./cmd/dataset                  # both
package main

import (
	"bufio"
	"compress/gzip"
	"flag"
	"fmt"
	"hash/fnv"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
)

// ---------- configuration ----------

const (
	webStartMonth   = 202505 // web events exist since 2025-05-27
	emailStartMonth = 202504 // 90d feature lookback before the first samples
	taskStartMonth  = 202601 // internal_events task/storage stream
	mauticEndDate   = "2026-05-28"
	internalEraDate = "2026-05-28"
	bulkThreshold   = 1000 // sends per (email_id, day) to call a campaign "bulk"
)

type config struct{ user, password, host, port string }

func loadConfig(path string) (*config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	cfg := &config{host: "127.0.0.1", port: "8123"}
	for line := range strings.SplitSeq(string(data), "\n") {
		key, val, ok := strings.Cut(strings.TrimSpace(line), "=")
		if !ok {
			continue
		}
		switch key {
		case "user":
			cfg.user = val
		case "password":
			cfg.password = val
		case "host":
			cfg.host = val
		case "http_port":
			cfg.port = val
		}
	}
	if cfg.user == "" || cfg.password == "" {
		return nil, fmt.Errorf("%s: user/password missing", path)
	}
	return cfg, nil
}

// queryToFile streams one TSV query result into path (atomic via .tmp).
func (c *config) queryToFile(client *http.Client, sql, path string) (int, error) {
	req, err := http.NewRequest(http.MethodPost, "http://"+c.host+":"+c.port+"/",
		strings.NewReader(sql+"\nFORMAT TabSeparated"))
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

	tmp := path + ".tmp"
	f, err := os.Create(tmp)
	if err != nil {
		return 0, err
	}
	w := bufio.NewWriterSize(f, 1<<20)
	lines := 0
	buf := make([]byte, 1<<20)
	for {
		n, rerr := resp.Body.Read(buf)
		if n > 0 {
			for _, b := range buf[:n] {
				if b == '\n' {
					lines++
				}
			}
			if _, werr := w.Write(buf[:n]); werr != nil {
				f.Close()
				return 0, werr
			}
		}
		if rerr == io.EOF {
			break
		}
		if rerr != nil {
			f.Close()
			return 0, rerr
		}
	}
	if err := w.Flush(); err != nil {
		f.Close()
		return 0, err
	}
	if err := f.Close(); err != nil {
		return 0, err
	}
	return lines, os.Rename(tmp, path)
}

// ---------- extract phase ----------

func monthsFrom(start int, now time.Time) []int {
	end := now.Year()*100 + int(now.Month())
	var out []int
	for m := start; m <= end; {
		out = append(out, m)
		if m%100 == 12 {
			m = m/100*100 + 101
		} else {
			m++
		}
	}
	return out
}

type extractJob struct{ file, sql string }

func extract(cfg *config, cacheDir string) error {
	if err := os.MkdirAll(cacheDir, 0o755); err != nil {
		return err
	}
	client := &http.Client{Timeout: 15 * time.Minute}
	now := time.Now().UTC()

	var jobs []extractJob
	for _, m := range monthsFrom(emailStartMonth, now) {
		jobs = append(jobs, extractJob{fmt.Sprintf("email_%d.tsv", m), fmt.Sprintf(`
SELECT user_id, toDate(created_at) AS d,
       countIf(event_type='email.sent' AND JSONExtractString(metadata,'src')='mautic')          AS mautic_sent,
       countIf(event_type='email.sent' AND JSONExtractString(metadata,'src')='internal_events') AS internal_sent,
       countIf(event_type='email.opened')  AS opened,
       countIf(event_type='email.clicked') AS clicked
FROM seedr_telemetry.user_telemetry_events
WHERE toYYYYMM(created_at)=%d
  AND event_type IN ('email.sent','email.opened','email.clicked')
  AND user_id IS NOT NULL
GROUP BY user_id, d`, m)})
	}
	for _, m := range monthsFrom(webStartMonth, now) {
		jobs = append(jobs, extractJob{fmt.Sprintf("web_%d.tsv", m), fmt.Sprintf(`
SELECT user_id, toDate(created_at) AS d,
       countIf(event_type='pageview') AS pageviews,
       countIf(event_type='event' AND category='File'    AND action='Download') AS file_dl,
       countIf(event_type='event' AND category='Archive' AND action='Download') AS archive_dl,
       countIf(event_type='event' AND category='File'    AND action='View')     AS file_views,
       countIf(event_type='event' AND category='video'   AND action IN ('stream_start','stream_session')) AS streams,
       countIf(event_type='pageview' AND (positionCaseInsensitive(url,'pricing')>0 OR positionCaseInsensitive(url,'payment')>0)) AS pricing_views,
       countIf(matomo_idgoal=4) AS goal4
FROM seedr_telemetry.user_telemetry_events
WHERE toYYYYMM(created_at)=%d
  AND surface IN ('web','landing')
  AND user_id IS NOT NULL
GROUP BY user_id, d`, m)})
	}
	for _, m := range monthsFrom(taskStartMonth, now) {
		jobs = append(jobs, extractJob{fmt.Sprintf("task_%d.tsv", m), fmt.Sprintf(`
SELECT user_id, toDate(created_at) AS d,
       countIf(event_type='task.completed')          AS completed,
       countIf(event_type='task.failed')             AS failed,
       countIf(event_type='account.storage_warning') AS storage_warnings
FROM seedr_telemetry.user_telemetry_events
WHERE toYYYYMM(created_at)=%d
  AND event_type IN ('task.completed','task.failed','account.storage_warning')
  AND user_id IS NOT NULL
GROUP BY user_id, d`, m)})
	}
	jobs = append(jobs,
		extractJob{"revenue.tsv", `
SELECT user_id, toUnixTimestamp(transaction_date) AS ts, amount_usd
FROM seedr_telemetry.revenue_facts
WHERE status='completed' AND user_id != 0
ORDER BY user_id, ts`},
		extractJob{"sub_events.tsv", `
SELECT user_id, toUnixTimestamp(created_at) AS ts, event_type
FROM seedr_telemetry.user_telemetry_events
WHERE event_type LIKE 'subscription.%' AND user_id IS NOT NULL
ORDER BY user_id, ts`},
		extractJob{"profile.tsv", `
SELECT user_id, anyHeavy(country) AS country,
       toUnixTimestamp(min(created_at)) AS first_seen,
       uniqExact(vid) AS devices
FROM seedr_telemetry.user_telemetry_events
WHERE user_id IS NOT NULL
GROUP BY user_id`},
		extractJob{"mobile.tsv", `
SELECT user_id, round(countIf(match(ua,'Mobile|Android'))/count(),3) AS mobile_share
FROM seedr_telemetry.user_telemetry_events
WHERE created_at >= today()-90 AND surface IN ('web','landing')
  AND user_id IS NOT NULL AND ua != ''
GROUP BY user_id`},
		extractJob{"send_detail.tsv", `
SELECT user_id, toDate(created_at) AS d,
       JSONExtractString(metadata,'email_id') AS email_id, count() AS c
FROM seedr_telemetry.user_telemetry_events
WHERE created_at >= '` + internalEraDate + `' AND event_type='email.sent' AND user_id IS NOT NULL
GROUP BY user_id, d, email_id`},
	)

	for i, j := range jobs {
		path := filepath.Join(cacheDir, j.file)
		if st, err := os.Stat(path); err == nil && st.Size() > 0 {
			fmt.Printf("skip  [%2d/%d] %-22s (cached)\n", i+1, len(jobs), j.file)
			continue
		}
		start := time.Now()
		lines, err := cfg.queryToFile(client, strings.TrimSpace(j.sql), path)
		if err != nil {
			return fmt.Errorf("%s: %w", j.file, err)
		}
		fmt.Printf("ok    [%2d/%d] %-22s %9d rows %6.1fs\n", i+1, len(jobs), j.file, lines, time.Since(start).Seconds())
	}
	return nil
}

// ---------- build phase: cache parsing ----------

var dayCache = map[string]int32{}

func parseDay(s string) int32 {
	if v, ok := dayCache[s]; ok {
		return v
	}
	t, err := time.Parse("2006-01-02", s)
	if err != nil {
		panic("bad date: " + s)
	}
	v := int32(t.Unix() / 86400)
	dayCache[s] = v
	return v
}

func atoi(s string) int64 {
	v, err := strconv.ParseInt(s, 10, 64)
	if err != nil {
		panic("bad int: " + s)
	}
	return v
}

func atof(s string) float64 {
	v, err := strconv.ParseFloat(s, 64)
	if err != nil {
		panic("bad float: " + s)
	}
	return v
}

type webRec struct {
	user                                                              uint64
	day                                                               int32
	pageviews, fileDl, archDl, fileViews, streams, pricingViews, goal4 int32
}

type emailRec struct {
	user                                       uint64
	day                                        int32
	mauticSent, internalSent, opened, clicked int32
}

type taskRec struct {
	user                            uint64
	day                             int32
	completed, failed, storageWarn int32
}

type txn struct {
	day    int32
	amount float32
}

type subEvent struct {
	day int32
	typ string
}

type profileRec struct {
	country     string
	firstDay    int32
	devices     int32
	mobileShare float32
}

func scanTSV(path string, fn func(fields []string)) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1<<20), 1<<20)
	for sc.Scan() {
		line := sc.Text()
		if line == "" {
			continue
		}
		fn(strings.Split(line, "\t"))
	}
	return sc.Err()
}

func scanGlob(cacheDir, prefix string, fn func(fields []string)) error {
	matches, err := filepath.Glob(filepath.Join(cacheDir, prefix+"_*.tsv"))
	if err != nil {
		return err
	}
	sort.Strings(matches)
	if len(matches) == 0 {
		return fmt.Errorf("no %s_*.tsv in %s (run -phase extract first)", prefix, cacheDir)
	}
	for _, m := range matches {
		if err := scanTSV(m, fn); err != nil {
			return fmt.Errorf("%s: %w", m, err)
		}
	}
	return nil
}

// userRange indexes a user's contiguous span in a (user,day)-sorted slice.
func buildIndex(n int, userAt func(int) uint64) map[uint64][2]int32 {
	idx := make(map[uint64][2]int32, 1<<20)
	for i := 0; i < n; {
		j := i
		u := userAt(i)
		for j < n && userAt(j) == u {
			j++
		}
		idx[u] = [2]int32{int32(i), int32(j)}
		i = j
	}
	return idx
}

// ---------- build phase: feature assembly ----------

type buildOpts struct {
	cacheDir, outPath string
	labelDays         int32
	negRate           float64
	minDate, maxDate  int32
	uplift            bool // also emit control rows (active no-email days) + a `treatment` column
}

const controlPerUser = 3 // control (no-email) days sampled per user in uplift mode

func build(o buildOpts) error {
	fmt.Println("loading caches...")

	var webs []webRec
	if err := scanGlob(o.cacheDir, "web", func(f []string) {
		webs = append(webs, webRec{uint64(atoi(f[0])), parseDay(f[1]),
			int32(atoi(f[2])), int32(atoi(f[3])), int32(atoi(f[4])),
			int32(atoi(f[5])), int32(atoi(f[6])), int32(atoi(f[7])), int32(atoi(f[8]))})
	}); err != nil {
		return err
	}

	var emails []emailRec
	if err := scanGlob(o.cacheDir, "email", func(f []string) {
		emails = append(emails, emailRec{uint64(atoi(f[0])), parseDay(f[1]),
			int32(atoi(f[2])), int32(atoi(f[3])), int32(atoi(f[4])), int32(atoi(f[5]))})
	}); err != nil {
		return err
	}

	var tasks []taskRec
	if err := scanGlob(o.cacheDir, "task", func(f []string) {
		tasks = append(tasks, taskRec{uint64(atoi(f[0])), parseDay(f[1]),
			int32(atoi(f[2])), int32(atoi(f[3])), int32(atoi(f[4]))})
	}); err != nil {
		return err
	}

	revenue := map[uint64][]txn{}
	if err := scanTSV(filepath.Join(o.cacheDir, "revenue.tsv"), func(f []string) {
		u := uint64(atoi(f[0]))
		revenue[u] = append(revenue[u], txn{int32(atoi(f[1]) / 86400), float32(atof(f[2]))})
	}); err != nil {
		return err
	}

	subs := map[uint64][]subEvent{}
	if err := scanTSV(filepath.Join(o.cacheDir, "sub_events.tsv"), func(f []string) {
		u := uint64(atoi(f[0]))
		subs[u] = append(subs[u], subEvent{int32(atoi(f[1]) / 86400), strings.TrimPrefix(f[2], "subscription.")})
	}); err != nil {
		return err
	}

	profiles := map[uint64]*profileRec{}
	if err := scanTSV(filepath.Join(o.cacheDir, "profile.tsv"), func(f []string) {
		profiles[uint64(atoi(f[0]))] = &profileRec{f[1], int32(atoi(f[2]) / 86400), int32(atoi(f[3])), 0}
	}); err != nil {
		return err
	}
	if err := scanTSV(filepath.Join(o.cacheDir, "mobile.tsv"), func(f []string) {
		if p, ok := profiles[uint64(atoi(f[0]))]; ok {
			p.mobileShare = float32(atof(f[1]))
		}
	}); err != nil {
		return err
	}

	// Internal-era marketing sends: bulk campaigns only.
	type sendKey struct {
		emailID string
		day     int32
	}
	bulkTotal := map[sendKey]int64{}
	type userSend struct {
		user    uint64
		day     int32
		emailID string
		c       int32
	}
	var details []userSend
	if err := scanTSV(filepath.Join(o.cacheDir, "send_detail.tsv"), func(f []string) {
		d := userSend{uint64(atoi(f[0])), parseDay(f[1]), f[2], int32(atoi(f[3]))}
		details = append(details, d)
		bulkTotal[sendKey{d.emailID, d.day}] += int64(d.c)
	}); err != nil {
		return err
	}
	bulkToday := map[uint64]map[int32]int32{} // user -> day -> bulk sends
	for _, d := range details {
		if bulkTotal[sendKey{d.emailID, d.day}] >= bulkThreshold {
			if bulkToday[d.user] == nil {
				bulkToday[d.user] = map[int32]int32{}
			}
			bulkToday[d.user][d.day] += d.c
		}
	}
	details = nil

	fmt.Printf("loaded: web=%d email=%d task=%d payers=%d profiles=%d\n",
		len(webs), len(emails), len(tasks), len(revenue), len(profiles))

	fmt.Println("sorting...")
	sort.Slice(webs, func(i, j int) bool {
		return webs[i].user < webs[j].user || (webs[i].user == webs[j].user && webs[i].day < webs[j].day)
	})
	sort.Slice(emails, func(i, j int) bool {
		return emails[i].user < emails[j].user || (emails[i].user == emails[j].user && emails[i].day < emails[j].day)
	})
	sort.Slice(tasks, func(i, j int) bool {
		return tasks[i].user < tasks[j].user || (tasks[i].user == tasks[j].user && tasks[i].day < tasks[j].day)
	})
	webIdx := buildIndex(len(webs), func(i int) uint64 { return webs[i].user })
	taskIdx := buildIndex(len(tasks), func(i int) uint64 { return tasks[i].user })

	mauticEnd := parseDay(mauticEndDate)

	out, err := os.Create(o.outPath)
	if err != nil {
		return err
	}
	defer out.Close()
	gz := gzip.NewWriter(out)
	w := bufio.NewWriterSize(gz, 1<<20)

	cols := []string{
		"user_id", "send_date", "era", "sends_today",
		"em_sent_7", "em_sent_30", "em_sent_90", "em_opened_30", "em_opened_90", "em_clicked_90",
		"em_open_rate_90", "days_since_last_send", "days_since_last_open",
		"web_active_days_7", "web_active_days_30", "pageviews_7", "pageviews_30",
		"file_dl_30", "file_dl_90", "archive_dl_30", "archive_dl_90", "file_views_30",
		"streams_30", "streams_90", "pricing_views_30", "pricing_views_90", "goal4_30", "goal4_90",
		"days_since_web_activity",
		"tasks_completed_30", "tasks_failed_30", "storage_warnings_30", "tasks_observable",
		"ever_paid", "ltv_before_usd", "txns_before", "days_since_last_txn", "last_txn_amount",
		"txns_365d", "premium_at_send",
		"last_sub_event", "days_since_sub_event", "subs_observable",
		"country", "tenure_days", "devices", "mobile_share_now",
		"seg_storage_warning", "seg_heavy_downloader", "seg_streamer", "seg_cart_abandoner",
		"seg_winback_active", "seg_dormant_payer", "seg_soft_cancel", "seg_monthly_loyal",
		"label_payment_14d", "label_conv_14d", "label_sub_started_14d",
		"days_to_payment", "first_payment_usd", "sample_weight",
	}
	if o.uplift {
		cols = append(cols, "treatment") // 1 = received a marketing email that day, 0 = control
	}
	fmt.Fprintln(w, strings.Join(cols, ","))

	// Live-verified stream starts (2026-06-11): task.* exists only since
	// 2026-05-25 and account.storage_warning since 2026-06-01 (the docs'
	// "internal_events since 2026-01-12" holds only for subscription.*).
	// tasks_observable=1 means the full 30d window is covered by the stream.
	taskObservableFrom := parseDay("2026-05-25") + 30
	subsObservableFrom := parseDay("2026-01-12")

	var nCandidates, nPositives, nRows, nControlRows int64
	segPos := map[string][2]int64{} // segment -> {samples, positives}

	hash := func(u uint64, d int32) uint64 {
		h := fnv.New64a()
		var b [12]byte
		for i := 0; i < 8; i++ {
			b[i] = byte(u >> (8 * i))
		}
		for i := 0; i < 4; i++ {
			b[8+i] = byte(uint32(d) >> (8 * i))
		}
		h.Write(b[:])
		return h.Sum64()
	}

	fmt.Println("assembling samples...")
	for ei := 0; ei < len(emails); {
		ej := ei
		u := emails[ei].user
		for ej < len(emails) && emails[ej].user == u {
			ej++
		}
		erecs := emails[ei:ej]
		ei = ej

		var wrecs []webRec
		if r, ok := webIdx[u]; ok {
			wrecs = webs[r[0]:r[1]]
		}
		var trecs []taskRec
		if r, ok := taskIdx[u]; ok {
			trecs = tasks[r[0]:r[1]]
		}
		txns := revenue[u]
		sevs := subs[u]
		prof := profiles[u]
		ub := bulkToday[u]

		// index days = email (treatment) days + sampled control (no-email
		// active) days. The feature block below runs identically for each, so
		// treatment and control features are computed by the SAME code.
		var indexDays []indexDay
		for _, er := range erecs {
			d := er.day
			if d < o.minDate || d > o.maxDate {
				continue
			}
			era, sendsToday := "", int32(0)
			if er.mauticSent > 0 && d <= mauticEnd {
				era, sendsToday = "mautic", er.mauticSent
			}
			if bt := ub[d]; bt > 0 {
				if era == "" {
					era = "internal"
				}
				sendsToday += bt
			}
			if era == "" {
				continue // transactional-only day, not a campaign sample
			}
			indexDays = append(indexDays, indexDay{d, sendsToday, era, 1})
		}
		if o.uplift {
			indexDays = append(indexDays, sampleControlDays(u, erecs, wrecs, o, hash)...)
		}

		for _, ix := range indexDays {
			d := ix.d
			era, sendsToday := ix.era, ix.sends
			if ix.treat == 1 {
				nCandidates++
			}

			// email history windows over [d-90, d-1] (strict < d; works for
			// control days too, which have no send on d)
			var s7, s30, s90, o30, o90, c90 int32
			lastSend, lastOpen := int32(-1), int32(-1)
			for k := len(erecs) - 1; k >= 0; k-- {
				e := erecs[k]
				if e.day >= d {
					continue
				}
				dd := d - e.day
				if dd > 90 {
					break
				}
				sent := e.mauticSent + e.internalSent
				if sent > 0 && lastSend < 0 {
					lastSend = dd
				}
				if e.opened > 0 && lastOpen < 0 {
					lastOpen = dd
				}
				s90 += sent
				o90 += e.opened
				c90 += e.clicked
				if dd <= 30 {
					s30 += sent
					o30 += e.opened
				}
				if dd <= 7 {
					s7 += sent
				}
			}
			openRate := 0.0
			if s90 > 0 {
				openRate = float64(o90) / float64(s90)
			}

			// web windows
			var wa7, wa30, pv7, pv30, fd30, fd90, ad30, ad90, fv30, st30, st90, pr30, pr90, g430, g490 int32
			lastWeb := int32(-1)
			for k := len(wrecs) - 1; k >= 0; k-- {
				wr := wrecs[k]
				if wr.day >= d {
					continue
				}
				dd := d - wr.day
				if dd > 90 {
					break
				}
				if lastWeb < 0 {
					lastWeb = dd
				}
				fd90 += wr.fileDl
				ad90 += wr.archDl
				st90 += wr.streams
				pr90 += wr.pricingViews
				g490 += wr.goal4
				if dd <= 30 {
					wa30++
					pv30 += wr.pageviews
					fd30 += wr.fileDl
					ad30 += wr.archDl
					fv30 += wr.fileViews
					st30 += wr.streams
					pr30 += wr.pricingViews
					g430 += wr.goal4
				}
				if dd <= 7 {
					wa7++
					pv7 += wr.pageviews
				}
			}

			// task windows
			var tc30, tf30, sw30 int32
			for k := len(trecs) - 1; k >= 0; k-- {
				tr := trecs[k]
				if tr.day >= d {
					continue
				}
				if d-tr.day > 30 {
					break
				}
				tc30 += tr.completed
				tf30 += tr.failed
				sw30 += tr.storageWarn
			}
			tasksObservable := boolToInt(d >= taskObservableFrom)

			// monetary state strictly before d
			var ltv float64
			var txnsBefore, txns365 int32
			lastTxnDay, lastAmount := int32(-1), float32(0)
			for _, t := range txns {
				if t.day >= d {
					break
				}
				ltv += float64(t.amount)
				txnsBefore++
				lastTxnDay = t.day
				lastAmount = t.amount
				if d-t.day <= 365 {
					txns365++
				}
			}
			everPaid := txnsBefore > 0
			daysSinceTxn := int32(-1)
			if everPaid {
				daysSinceTxn = d - lastTxnDay
			}
			// premium proxy: monthly cadence (txn within 35d) or an
			// annual-sized payment (>= $60) within 370d
			premium := everPaid && (daysSinceTxn <= 35 || (lastAmount >= 60 && daysSinceTxn <= 370))

			// subscription lifecycle state before d
			lastSubEvent, daysSinceSub := "", int32(-1)
			for _, se := range sevs {
				if se.day >= d {
					break
				}
				lastSubEvent, daysSinceSub = se.typ, d-se.day
			}
			subsObservable := boolToInt(d >= subsObservableFrom)

			// labels: first payment in (d, d+labelDays]
			labelPay, labelConv, labelSub := 0, 0, 0
			daysToPay, firstPayUSD := int32(-1), float32(0)
			for _, t := range txns {
				if t.day <= d {
					continue
				}
				if t.day-d <= o.labelDays {
					labelPay = 1
					daysToPay = t.day - d
					firstPayUSD = t.amount
				}
				break
			}
			if labelPay == 1 && !premium {
				labelConv = 1
			}
			for _, se := range sevs {
				if se.day > d && se.day-d <= o.labelDays && (se.typ == "created" || se.typ == "reactivated") {
					labelSub = 1
					break
				}
			}

			// historical segment flags (best effort, see docs)
			segs := map[string]bool{
				"seg_storage_warning":  sw30 > 0,
				"seg_heavy_downloader": fd30+ad30 > 50,
				"seg_streamer":         st30 >= 5,
				"seg_cart_abandoner":   pr30 > 0 && g430 == 0 && !premium,
				"seg_winback_active":   everPaid && !premium && wa30 > 0,
				"seg_dormant_payer":    everPaid && !premium && daysSinceTxn >= 0 && daysSinceTxn <= 180 && wa30 == 0,
				"seg_soft_cancel":      lastSubEvent == "cancellation_scheduled" && daysSinceSub <= 60,
				"seg_monthly_loyal":    premium && txns365 >= 3 && lastAmount < 30,
			}

			if ix.treat == 1 {
				if labelPay == 1 {
					nPositives++
				}
				for name, on := range segs {
					if on {
						c := segPos[name]
						c[0]++
						c[1] += int64(labelPay)
						segPos[name] = c
					}
				}
			}

			// negative downsampling (deterministic)
			weight := 1.0
			if labelPay == 0 {
				if float64(hash(u, d)%10000) >= o.negRate*10000 {
					continue
				}
				weight = 1.0 / o.negRate
			}
			nRows++

			country, tenure, devices, mobile := "", int32(-1), int32(0), float32(0)
			if prof != nil {
				country, devices, mobile = prof.country, prof.devices, prof.mobileShare
				first := prof.firstDay
				if everPaid && txns[0].day < first {
					first = txns[0].day
				}
				tenure = d - first
			}

			row := []string{
				strconv.FormatUint(u, 10),
				time.Unix(int64(d)*86400, 0).UTC().Format("2006-01-02"),
				era,
				itoa(sendsToday),
				itoa(s7), itoa(s30), itoa(s90), itoa(o30), itoa(o90), itoa(c90),
				fmt.Sprintf("%.4f", openRate), itoa(lastSend), itoa(lastOpen),
				itoa(wa7), itoa(wa30), itoa(pv7), itoa(pv30),
				itoa(fd30), itoa(fd90), itoa(ad30), itoa(ad90), itoa(fv30),
				itoa(st30), itoa(st90), itoa(pr30), itoa(pr90), itoa(g430), itoa(g490),
				itoa(lastWeb),
				itoa(tc30), itoa(tf30), itoa(sw30), strconv.Itoa(tasksObservable),
				strconv.Itoa(boolToInt(everPaid)),
				fmt.Sprintf("%.2f", ltv), itoa(txnsBefore), itoa(daysSinceTxn),
				fmt.Sprintf("%.2f", lastAmount), itoa(txns365),
				strconv.Itoa(boolToInt(premium)),
				lastSubEvent, itoa(daysSinceSub), strconv.Itoa(subsObservable),
				country, itoa(tenure), itoa(devices), fmt.Sprintf("%.3f", mobile),
				b2s(segs["seg_storage_warning"]), b2s(segs["seg_heavy_downloader"]),
				b2s(segs["seg_streamer"]), b2s(segs["seg_cart_abandoner"]),
				b2s(segs["seg_winback_active"]), b2s(segs["seg_dormant_payer"]),
				b2s(segs["seg_soft_cancel"]), b2s(segs["seg_monthly_loyal"]),
				strconv.Itoa(labelPay), strconv.Itoa(labelConv), strconv.Itoa(labelSub),
				itoa(daysToPay), fmt.Sprintf("%.2f", firstPayUSD),
				fmt.Sprintf("%.4f", weight),
			}
			if o.uplift {
				row = append(row, strconv.Itoa(ix.treat))
				if ix.treat == 0 {
					nControlRows++
				}
			}
			fmt.Fprintln(w, strings.Join(row, ","))
		}
	}

	if err := w.Flush(); err != nil {
		return err
	}
	if err := gz.Close(); err != nil {
		return err
	}

	fmt.Printf("\ncandidate samples: %d\npositives (payment<=%dd): %d (%.3f%%)\nrows written (after %.0f%% negative sampling): %d\n",
		nCandidates, o.labelDays, nPositives,
		100*float64(nPositives)/float64(max(nCandidates, 1)),
		o.negRate*100, nRows)
	fmt.Println("\nper-segment positive rate (on ALL candidates, pre-sampling):")
	names := make([]string, 0, len(segPos))
	for n := range segPos {
		names = append(names, n)
	}
	sort.Strings(names)
	for _, n := range names {
		c := segPos[n]
		fmt.Printf("  %-22s %9d samples  %6d pos  %.3f%%\n", n, c[0], c[1], 100*float64(c[1])/float64(max(c[0], 1)))
	}
	if o.uplift {
		fmt.Printf("\nuplift mode: %d control rows written (treatment=0), rest are treatment=1\n", nControlRows)
	}
	fmt.Println("\noutput:", o.outPath)
	return nil
}

// indexDay is a (day, treatment) record the feature builder runs on: email
// days (treat=1) and sampled control days (treat=0).
type indexDay struct {
	d, sends int32
	era      string
	treat    int
}

// sampleControlDays picks up to controlPerUser web-active days for a user that
// have NO marketing email within ±14 days — a quasi-control "the user was
// active but we did not email them then". Deterministic via the hash.
func sampleControlDays(u uint64, erecs []emailRec, wrecs []webRec, o buildOpts,
	hash func(uint64, int32) uint64) []indexDay {
	type cd struct {
		d int32
		h uint64
	}
	var elig []cd
	for _, wr := range wrecs {
		d := wr.day
		if d < o.minDate || d > o.maxDate {
			continue
		}
		near := false
		for _, e := range erecs {
			if e.mauticSent+e.internalSent > 0 {
				dd := e.day - d
				if dd < 0 {
					dd = -dd
				}
				if dd <= 14 {
					near = true
					break
				}
			}
		}
		if near {
			continue
		}
		elig = append(elig, cd{d, hash(u, d)})
	}
	sort.Slice(elig, func(i, j int) bool { return elig[i].h < elig[j].h })
	if len(elig) > controlPerUser {
		elig = elig[:controlPerUser]
	}
	out := make([]indexDay, len(elig))
	for i, e := range elig {
		out[i] = indexDay{e.d, 0, "control", 0}
	}
	return out
}

func itoa(v int32) string { return strconv.FormatInt(int64(v), 10) }

func boolToInt(b bool) int {
	if b {
		return 1
	}
	return 0
}

func b2s(b bool) string { return strconv.Itoa(boolToInt(b)) }

// ---------- main ----------

func main() {
	var (
		phase     = flag.String("phase", "all", "extract | build | all")
		cacheDir  = flag.String("cache", "dataset_cache", "cache directory for raw extracts")
		outPath   = flag.String("out", "train_email_conversion.csv.gz", "output training set")
		cfgPath   = flag.String("config", filepath.Join(os.Getenv("HOME"), ".clickhouse.seedr"), "credentials file")
		labelDays = flag.Int("label-days", 14, "conversion window after send, days")
		negRate   = flag.Float64("neg-rate", 0.25, "fraction of negative samples to keep (sample_weight compensates)")
		minDate   = flag.String("min-date", "2025-06-26", "first send date (default: 30d after web data starts)")
		uplift    = flag.Bool("uplift", false, "also emit control (no-email active) rows + a `treatment` column")
	)
	flag.Parse()
	if *uplift && *outPath == "train_email_conversion.csv.gz" {
		*outPath = "train_uplift.csv.gz"
	}

	if *phase == "extract" || *phase == "all" {
		cfg, err := loadConfig(*cfgPath)
		if err != nil {
			fmt.Fprintln(os.Stderr, "error:", err)
			os.Exit(1)
		}
		if err := extract(cfg, *cacheDir); err != nil {
			fmt.Fprintln(os.Stderr, "extract error:", err)
			os.Exit(1)
		}
	}
	if *phase == "build" || *phase == "all" {
		maxD := int32(time.Now().UTC().Unix()/86400) - int32(*labelDays) - 1
		err := build(buildOpts{
			cacheDir:  *cacheDir,
			outPath:   *outPath,
			labelDays: int32(*labelDays),
			negRate:   *negRate,
			minDate:   parseDay(*minDate),
			maxDate:   maxD,
			uplift:    *uplift,
		})
		if err != nil {
			fmt.Fprintln(os.Stderr, "build error:", err)
			os.Exit(1)
		}
	}
}
