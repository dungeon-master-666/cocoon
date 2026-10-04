// Synthetic, controllable inference backend for local development only.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"strconv"
	"strings"
	"time"
)

var scenarios = map[string]bool{
	"normal": true, "delay-headers": true, "delay-body": true, "hang": true,
	"http-error": true, "disconnect-before-headers": true, "disconnect-after-headers": true,
	"disconnect-mid-stream": true, "incomplete-json": true, "incomplete-sse": true,
	"invalid-json-tail": true, "json-error": true, "empty-json": true,
	"sse-error": true, "malformed-sse": true, "incomplete-event": true,
	"disconnect-after-usage": true, "disconnect-after-done": true, "duplicate-done": true,
	"http-client-error": true, "http-text-error": true, "empty-http-error": true, "no-content": true,
}

type backend struct {
	scenario   string
	delay      time.Duration
	chunks     int
	bytes      int
	chunkDelay time.Duration
}

func writeJSON(w http.ResponseWriter, value any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(value)
}

func pause(r *http.Request, delay time.Duration) bool {
	select {
	case <-time.After(delay):
		return true
	case <-r.Context().Done():
		return false
	}
}

// Hijack closes the connection without the final HTTP chunk, including when no
// headers have been sent. Returning normally would emit a successful HTTP EOF.
func disconnect(w http.ResponseWriter) {
	conn, _, err := w.(http.Hijacker).Hijack()
	if err == nil {
		_ = conn.Close()
	}
}

func positiveQuery(r *http.Request, name string, fallback int) int {
	value, err := strconv.Atoi(r.URL.Query().Get(name))
	if err == nil && value > 0 {
		return value
	}
	return fallback
}

func (b backend) completion(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST required", http.StatusMethodNotAllowed)
		return
	}
	defer r.Body.Close()
	var request struct {
		Stream bool   `json:"stream"`
		Model  string `json:"model"`
	}
	if r.URL.Path == "/v1/audio/transcriptions" {
		// Keep the existing load-test endpoint; it is not an audio simulator.
		_, _ = io.Copy(io.Discard, r.Body)
		request.Stream = true
	} else if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&request); err != nil {
		http.Error(w, "invalid JSON request", http.StatusBadRequest)
		return
	}
	if request.Model == "" {
		request.Model = "Qwen/Qwen3-8B"
	}
	if (b.scenario == "incomplete-json" && request.Stream) ||
		((b.scenario == "incomplete-sse" || b.scenario == "disconnect-mid-stream") && !request.Stream) {
		http.Error(w, "scenario is incompatible with stream mode", http.StatusBadRequest)
		return
	}
	log.Printf("request scenario=%s stream=%t", b.scenario, request.Stream)
	if b.scenario != "normal" {
		log.Printf("fault=%s", b.scenario)
	}
	if b.scenario == "disconnect-before-headers" {
		disconnect(w)
		return
	}
	if b.scenario == "hang" {
		<-r.Context().Done()
		return
	}
	if b.scenario == "delay-headers" && !pause(r, b.delay) {
		return
	}
	if b.scenario == "http-error" || b.scenario == "http-client-error" {
		w.Header().Set("Content-Type", "application/json")
		status := http.StatusServiceUnavailable
		if b.scenario == "http-client-error" {
			status = http.StatusBadRequest
		}
		w.WriteHeader(status)
		writeJSON(w, map[string]any{"error": map[string]string{"message": "injected backend error", "type": "backend_error"},
			"usage": map[string]int{"prompt_tokens": 34, "completion_tokens": 100}})
		return
	}
	if b.scenario == "http-text-error" {
		http.Error(w, "injected backend error", 503)
		return
	}
	if b.scenario == "empty-http-error" {
		w.WriteHeader(503)
		return
	}
	if b.scenario == "no-content" {
		w.WriteHeader(204)
		return
	}
	if request.Stream {
		w.Header().Set("Content-Type", "text/event-stream")
	} else {
		w.Header().Set("Content-Type", "application/json")
	}
	w.Header().Set("Cache-Control", "no-cache")
	w.WriteHeader(http.StatusOK)
	flusher := w.(http.Flusher)
	flusher.Flush()
	if b.scenario == "disconnect-after-headers" {
		disconnect(w)
		return
	}
	if b.scenario == "delay-body" && !pause(r, b.delay) {
		return
	}
	if b.scenario == "incomplete-json" {
		_, _ = io.WriteString(w, `{"choices":[`)
		return
	}
	if b.scenario == "empty-json" {
		return
	}
	if b.scenario == "json-error" {
		writeJSON(w, map[string]any{"error": map[string]string{"message": "injected backend error"}})
		return
	}
	usage := map[string]any{
		"prompt_tokens": 34, "completion_tokens": 100, "total_tokens": 134,
		"prompt_tokens_details":     map[string]int{"cached_tokens": 11},
		"completion_tokens_details": map[string]int{"reasoning_tokens": 10},
	}
	if !request.Stream {
		choice := map[string]any{"index": 0, "finish_reason": "stop"}
		object := "chat.completion"
		if r.URL.Path == "/v1/completions" {
			choice["text"] = "local smoke response"
			object = "text_completion"
		} else {
			choice["message"] = map[string]string{"role": "assistant", "content": "local smoke response"}
		}
		writeJSON(w, map[string]any{"id": "local-smoke", "object": object, "created": 1,
			"model": request.Model, "choices": []any{choice}, "usage": usage})
		if b.scenario == "invalid-json-tail" {
			_, _ = io.WriteString(w, "garbage")
		}
		return
	}
	chunks := positiveQuery(r, "chunks", b.chunks)
	bytesPer := positiveQuery(r, "bytes", b.bytes)
	// Retain the existing benchmark query parameters.
	chunkDelay := b.chunkDelay
	if delayMs, err := strconv.Atoi(r.URL.Query().Get("delay_ms")); err == nil && delayMs >= 0 {
		chunkDelay = time.Duration(delayMs) * time.Millisecond
	}
	send := func(choices []any, usage any) bool {
		value := map[string]any{"id": "local-smoke", "object": "chat.completion.chunk", "created": 1,
			"model": request.Model, "choices": choices, "usage": usage}
		if r.URL.Path == "/v1/completions" {
			value["object"] = "text_completion"
		}
		data, _ := json.Marshal(value)
		_, err := fmt.Fprintf(w, "data: %s\n\n", data)
		flusher.Flush()
		return err == nil
	}
	for i := 0; i < chunks; i++ {
		choice := map[string]any{"index": 0, "delta": map[string]string{"content": strings.Repeat("x", bytesPer)}, "finish_reason": nil}
		if r.URL.Path == "/v1/completions" {
			delete(choice, "delta")
			choice["text"] = strings.Repeat("x", bytesPer)
		}
		if !send([]any{choice}, nil) {
			return
		}
		if i == 1 && b.scenario == "disconnect-mid-stream" {
			disconnect(w)
			return
		}
		if chunkDelay > 0 && !pause(r, chunkDelay) {
			return
		}
	}
	if b.scenario == "incomplete-sse" {
		return // Valid HTTP framing, but missing terminal SSE event and usage.
	}
	finalChoice := map[string]any{"index": 0, "delta": map[string]string{}, "finish_reason": "stop"}
	if r.URL.Path == "/v1/completions" {
		delete(finalChoice, "delta")
		finalChoice["text"] = ""
	}
	if !send([]any{finalChoice}, nil) || !send([]any{}, usage) {
		return
	}
	switch b.scenario {
	case "disconnect-after-usage":
		disconnect(w)
		return
	case "sse-error":
		_, _ = io.WriteString(w, "event: error\ndata: {\"error\":{\"message\":\"injected backend error\"}}\n\n")
		return
	case "malformed-sse":
		_, _ = io.WriteString(w, "data: {invalid}\n\n")
		return
	case "incomplete-event":
		_, _ = io.WriteString(w, "data: {\"choices\":[]}")
		return
	}
	_, _ = io.WriteString(w, "data: [DONE]\n\n")
	flusher.Flush()
	if b.scenario == "disconnect-after-done" {
		disconnect(w)
		return
	}
	if b.scenario == "duplicate-done" {
		_, _ = io.WriteString(w, "data: [DONE]\n\n")
	}
}

func (b backend) handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, map[string]string{"status": "ok", "scenario": b.scenario})
	})
	mux.HandleFunc("/v1/models", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, map[string]any{"object": "list", "data": []any{map[string]string{"id": "Qwen/Qwen3-8B", "object": "model"}}})
	})
	for _, path := range []string{"/v1/chat/completions", "/v1/completions", "/v1/audio/transcriptions"} {
		mux.HandleFunc(path, b.completion)
	}
	return mux
}

func main() {
	listen := flag.String("listen", "127.0.0.1:8000", "HTTP listen address")
	scenario := flag.String("scenario", "normal", "normal, delay-headers, delay-body, hang, http-error, disconnect-before-headers, disconnect-after-headers, disconnect-mid-stream, incomplete-json, incomplete-sse")
	delay := flag.Duration("delay", time.Second, "delay for delay-headers/delay-body")
	chunks := flag.Int("chunks", 50, "SSE content chunks (at least 2)")
	bytesPer := flag.Int("bytes", 128, "bytes per SSE content chunk")
	chunkDelay := flag.Duration("chunk-delay", 0, "delay between SSE content chunks")
	flag.Parse()
	if !scenarios[*scenario] || *delay < 0 || *chunkDelay < 0 || *chunks < 2 || *bytesPer < 1 {
		log.Fatal("invalid scenario, delay, chunks or bytes")
	}
	b := backend{*scenario, *delay, *chunks, *bytesPer, *chunkDelay}
	log.Printf("listening=%s scenario=%s", *listen, *scenario)
	log.Fatal(http.ListenAndServe(*listen, b.handler()))
}
