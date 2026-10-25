package trace

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"sync/atomic"
	"time"
)

// Client is a minimal JSON-RPC client for debug_traceBlockByNumber.
type Client struct {
	URL           string
	HTTP          *http.Client
	Retries       int
	TracerTimeout string // optional tracer "timeout" (e.g. "120s"); empty = node default
	id            atomic.Uint64
}

func (c *Client) redacted() string {
	u, err := url.Parse(c.URL)
	if err != nil {
		return "<rpc>"
	}
	return u.Scheme + "://" + u.Host
}

// NewClient creates a client with a pooled transport sized for workers.
func NewClient(url string, workers int, timeout time.Duration) *Client {
	tr := &http.Transport{
		Proxy:               http.ProxyFromEnvironment,
		DialContext:         (&net.Dialer{Timeout: 10 * time.Second, KeepAlive: 30 * time.Second}).DialContext,
		MaxIdleConns:        workers * 2,
		MaxIdleConnsPerHost: workers * 2,
		IdleConnTimeout:     90 * time.Second,
		DisableCompression:  false,
	}
	return &Client{URL: url, HTTP: &http.Client{Transport: tr, Timeout: timeout}, Retries: 5}
}

type rpcErr struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

type rpcResp struct {
	Result json.RawMessage `json:"result"`
	Error  *rpcErr         `json:"error"`
}

// RPCError is a JSON-RPC level error.
type RPCError struct {
	Code    int
	Message string
}

func (e *RPCError) Error() string { return fmt.Sprintf("rpc error %d: %s", e.Code, e.Message) }

type countingReader struct {
	r io.Reader
	n int64
}

func (c *countingReader) Read(p []byte) (int, error) {
	n, err := c.r.Read(p)
	c.n += int64(n)
	return n, err
}

func (c *Client) traceOnce(ctx context.Context, block uint64) ([]TxTrace, int64, error) {
	cfg := map[string]any{"tracer": "callTracer", "tracerConfig": map[string]any{"onlyTopCall": false}}
	if c.TracerTimeout != "" {
		cfg["timeout"] = c.TracerTimeout
	}
	body, _ := json.Marshal(map[string]any{"jsonrpc": "2.0", "id": c.id.Add(1), "method": "debug_traceBlockByNumber", "params": []any{fmt.Sprintf("0x%x", block), cfg}})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.URL, bytes.NewReader(body))
	if err != nil {
		return nil, 0, err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.HTTP.Do(req)
	if err != nil {
		// net/http errors embed the full URL (query strings may carry API
		// keys): re-wrap with scheme://host only.
		var ue *url.Error
		if errors.As(err, &ue) {
			return nil, 0, fmt.Errorf("%s %s: %w", ue.Op, c.redacted(), ue.Err)
		}
		return nil, 0, err
	}
	defer resp.Body.Close()
	cr := &countingReader{r: resp.Body}
	if resp.StatusCode != http.StatusOK {
		snippet, _ := io.ReadAll(io.LimitReader(cr, 512))
		return nil, cr.n, fmt.Errorf("http %d: %s", resp.StatusCode, strings.TrimSpace(string(snippet)))
	}
	var r rpcResp
	if err := json.NewDecoder(cr).Decode(&r); err != nil {
		return nil, cr.n, fmt.Errorf("decode response: %w", err)
	}
	if r.Error != nil {
		return nil, cr.n, &RPCError{Code: r.Error.Code, Message: r.Error.Message}
	}
	var traces []TxTrace
	if err := json.Unmarshal(r.Result, &traces); err != nil {
		return nil, cr.n, fmt.Errorf("decode traces: %w", err)
	}
	return traces, cr.n, nil
}

// TraceBlock calls debug_traceBlockByNumber with retries (exponential
// backoff). It returns the traces, the latency of the successful attempt,
// bytes received and the number of retries used.
func (c *Client) TraceBlock(ctx context.Context, block uint64) ([]TxTrace, time.Duration, int64, int, error) {
	var last error
	var total int64
	for attempt := 0; attempt <= c.Retries; attempt++ {
		if attempt > 0 {
			d := time.Duration(1<<min(attempt-1, 6)) * 250 * time.Millisecond
			select {
			case <-time.After(d):
			case <-ctx.Done():
				return nil, 0, total, attempt, ctx.Err()
			}
		}
		t0 := time.Now()
		tr, n, err := c.traceOnce(ctx, block)
		total += n
		if err == nil {
			return tr, time.Since(t0), total, attempt, nil
		}
		last = err
		if ctx.Err() != nil {
			return nil, 0, total, attempt, ctx.Err()
		}
		var re *RPCError
		if errors.As(err, &re) && re.Code == -32601 {
			return nil, 0, total, attempt, err // method not found: not retryable
		}
	}
	return nil, 0, total, c.Retries, last
}
