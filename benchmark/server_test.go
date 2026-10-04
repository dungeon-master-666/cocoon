package main

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestBackendScenarios(t *testing.T) {
	for scenario := range scenarios {
		t.Run(scenario, func(t *testing.T) {
			server := httptest.NewServer(backend{scenario, 50 * time.Millisecond, 3, 8, 0}.handler())
			defer server.Close()
			client := &http.Client{Timeout: 200 * time.Millisecond}
			// Fault selection never disables readiness.
			resp, err := client.Get(server.URL + "/v1/models")
			if err != nil || resp.StatusCode != 200 {
				t.Fatalf("readiness: %v, %v", resp, err)
			}
			resp.Body.Close()
			start := time.Now()
			stream := scenario != "incomplete-json" && scenario != "invalid-json-tail" && scenario != "empty-json" && scenario != "json-error"
			resp, err = client.Post(server.URL+"/v1/chat/completions", "application/json",
				strings.NewReader(fmt.Sprintf(`{"stream":%t}`, stream)))
			if scenario == "disconnect-before-headers" || scenario == "hang" {
				if err == nil {
					resp.Body.Close()
					t.Fatal("expected failure before headers")
				}
				if scenario == "hang" && !strings.Contains(err.Error(), "Client.Timeout") {
					t.Fatalf("expected deadline: %v", err)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			defer resp.Body.Close()
			headersTime := time.Since(start)
			body, readErr := io.ReadAll(resp.Body)
			switch scenario {
			case "disconnect-after-headers", "disconnect-mid-stream", "disconnect-after-usage", "disconnect-after-done":
				if readErr != io.ErrUnexpectedEOF {
					t.Fatalf("expected broken HTTP framing, got %v", readErr)
				}
				if scenario == "disconnect-mid-stream" && strings.Count(string(body), "data: ") != 2 {
					t.Fatalf("expected exactly two events: %s", body)
				}
				if scenario == "disconnect-after-headers" && len(body) != 0 {
					t.Fatalf("unexpected body: %s", body)
				}
				if scenario == "disconnect-after-done" && !strings.HasSuffix(string(body), "data: [DONE]\n\n") {
					t.Fatalf("terminal marker must precede framing failure: %s", body)
				}
				if scenario == "disconnect-after-usage" && (!strings.Contains(string(body), `"total_tokens":134`) || strings.Contains(string(body), "[DONE]")) {
					t.Fatalf("expected usage without terminal: %s", body)
				}
			case "incomplete-json", "invalid-json-tail", "empty-json":
				if readErr != nil || json.Valid(body) {
					t.Fatalf("expected full HTTP with invalid JSON: %s, %v", body, readErr)
				}
			case "incomplete-sse":
				if readErr != nil || strings.Contains(string(body), "[DONE]") || strings.Count(string(body), "data: ") != 3 {
					t.Fatalf("expected full HTTP with unterminated SSE: %s, %v", body, readErr)
				}
			case "http-error", "http-client-error", "http-text-error":
				status := 503
				if scenario == "http-client-error" {
					status = 400
				}
				if readErr != nil || resp.StatusCode != status || !strings.Contains(string(body), "injected backend error") {
					t.Fatalf("expected backend error: %d %s", resp.StatusCode, body)
				}
			case "empty-http-error", "no-content":
				status := 503
				if scenario == "no-content" {
					status = 204
				}
				if readErr != nil || resp.StatusCode != status || len(body) != 0 {
					t.Fatalf("bad empty response: %d %s %v", resp.StatusCode, body, readErr)
				}
			case "json-error", "sse-error":
				if readErr != nil || !strings.Contains(string(body), "injected backend error") || strings.Contains(string(body), "[DONE]") {
					t.Fatalf("bad error body: %s %v", body, readErr)
				}
				if scenario == "json-error" && !json.Valid(body) {
					t.Fatalf("invalid JSON error: %s", body)
				}
				if scenario == "sse-error" && !strings.Contains(string(body), "event: error\n") {
					t.Fatalf("missing error event: %s", body)
				}
			case "malformed-sse", "incomplete-event":
				if readErr != nil || strings.Contains(string(body), "[DONE]") {
					t.Fatalf("bad SSE fault: %s %v", body, readErr)
				}
				if scenario == "malformed-sse" && !strings.HasSuffix(string(body), "data: {invalid}\n\n") {
					t.Fatalf("missing invalid event: %s", body)
				}
				if scenario == "incomplete-event" && !strings.HasSuffix(string(body), `data: {"choices":[]}`) {
					t.Fatalf("missing unfinished event: %s", body)
				}
			case "duplicate-done":
				if readErr != nil || strings.Count(string(body), "data: [DONE]\n\n") != 2 {
					t.Fatalf("missing duplicate terminal: %s %v", body, readErr)
				}
			default:
				if readErr != nil || !strings.HasSuffix(string(body), "data: [DONE]\n\n") || strings.Count(string(body), "data: ") != 6 {
					t.Fatalf("bad SSE: %s, %v", body, readErr)
				}
				if strings.HasPrefix(scenario, "delay-") && time.Since(start) < 45*time.Millisecond {
					t.Fatal("delay not applied")
				}
				if scenario == "delay-headers" && headersTime < 45*time.Millisecond {
					t.Fatal("headers were not delayed")
				}
				if scenario == "delay-body" && time.Since(start)-headersTime < 45*time.Millisecond {
					t.Fatal("body was not delayed after headers")
				}
			}
		})
	}
}

func TestJSONAndStreamContent(t *testing.T) {
	server := httptest.NewServer(backend{"normal", 0, 3, 8, 0}.handler())
	defer server.Close()
	for _, path := range []string{"/v1/chat/completions", "/v1/completions"} {
		resp, err := http.Post(server.URL+path, "application/json", strings.NewReader(`{"stream":false}`))
		if err != nil {
			t.Fatal(err)
		}
		var body map[string]any
		err = json.NewDecoder(resp.Body).Decode(&body)
		resp.Body.Close()
		if err != nil || resp.StatusCode != 200 || body["usage"].(map[string]any)["total_tokens"] != float64(134) {
			t.Fatalf("JSON/usage: %v, %v", body, err)
		}
		choice := body["choices"].([]any)[0].(map[string]any)
		content := choice["text"]
		if path == "/v1/chat/completions" {
			content = choice["message"].(map[string]any)["content"]
		}
		if content != "local smoke response" {
			t.Fatalf("wrong content: %v", content)
		}
	}
}
