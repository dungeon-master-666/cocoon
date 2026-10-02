package main

import (
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"strconv"
	"time"
)

func stream(w http.ResponseWriter, r *http.Request) {
  defer r.Body.Close()
  body, _ := io.ReadAll(r.Body)
  bodyString := string(body)

  if false {
    fmt.Println("body=", bodyString)
  }

	q := r.URL.Query()
	chunks, _ := strconv.Atoi(q.Get("chunks"))
	if chunks <= 0 {
		chunks = 50
	}
	bytesPer, _ := strconv.Atoi(q.Get("bytes"))
	if bytesPer <= 0 {
		bytesPer = 128
	}
	delayMs, _ := strconv.Atoi(q.Get("delay_ms"))

	w.Header().Set("Content-Type", "application/json")
	flusher, _ := w.(http.Flusher)

  fmt.Fprintf(w, `{"chunks":%d,"bytes":%d,"delay":%d}`+"\n", chunks, bytesPer, delayMs)
	for i := 0; i < chunks; i++ {
		fmt.Fprintf(w, `{"delta":"%0*s","i":%d}`+"\n", bytesPer, "", i)
		flusher.Flush()
		if delayMs > 0 {
			time.Sleep(time.Duration(delayMs) * time.Millisecond)
		}
	}
  fmt.Fprintf(w, `{"usage":{"prompt_tokens":34,"total_tokens":134,"completion_tokens":100,"completion_tokens_details":{"reasoning_tokens":10},"prompt_tokens_details":{"cached_tokens":11}}}`+"\n")
	flusher.Flush()
}

func nostream(w http.ResponseWriter, r *http.Request) {
  defer r.Body.Close()
  io.ReadAll(r.Body)

	w.Header().Set("Content-Type", "application/json")
	flusher, _ := w.(http.Flusher)

  fmt.Fprintf(w, "{}\n")
	flusher.Flush()
}

func main() {
	listen := flag.String("listen", ":8000", "HTTP listen address")
	flag.Parse()
	http.HandleFunc("/v1/chat/completions", stream)
	http.HandleFunc("/v1/audio/transcriptions", stream)
	http.HandleFunc("/v1/completions", stream)
	http.HandleFunc("/v1/models", nostream)
	fmt.Printf("Server listening on %s\n", *listen)
	log.Fatal(http.ListenAndServe(*listen, nil))
}
