// Fifth fixture application: a Go inventory API, standard library only.
//
// Two reasons it exists (ADR-0003 wants ground truth that keeps widening):
//
//  1. A fourth language. buggy-shop is FastAPI, task-tracker Flask, notes-api
//     plain Node.js, library-graphql GraphQL on Flask. Go fails differently from
//     all of them: a nil map entry is a nil pointer, an unchecked type assertion
//     panics, and a panic in a handler is recovered by middleware into a 500.
//  2. Query parameters. Every generator rule before this fixture varied the
//     body, the path or the credentials, and always sent required query
//     parameters with valid values. BUG-501 and BUG-502 live entirely in the
//     query string, and are what case_missing_required_query and
//     case_wrong_query_type (modules/generator/rules.py) were written to reach.
//     GET /warehouses and GET /warehouses/lookup are the same two shapes done
//     correctly, so those rules' false-positive rate is measured, not assumed.
//
// Every bug here is recorded in seeded_defects.yaml. Do not "fix" the bugs in
// this file - they are the specification. Run it with:
//
//	go run .
package main

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"runtime/debug"
	"sort"
	"strconv"
	"strings"
	"sync"
)

const addr = ":8084"

// Fixture-only static token, same simplification task-tracker makes with its
// TOKENS map. A real deployment reads credentials from a secret store.
const token = "inventory-fixture-token"

const pageSize = 10

type Item struct {
	ID        int    `json:"id"`
	SKU       string `json:"sku"`
	Name      string `json:"name"`
	Quantity  int    `json:"quantity"`
	Warehouse string `json:"warehouse"`
}

type Warehouse struct {
	Code string `json:"code"`
	City string `json:"city"`
}

var (
	mu     sync.Mutex
	nextID = 4
	items  = map[int]*Item{
		1: {ID: 1, SKU: "BOLT-M8", Name: "Hex bolt M8", Quantity: 1200, Warehouse: "AMS"},
		2: {ID: 2, SKU: "NUT-M8", Name: "Hex nut M8", Quantity: 900, Warehouse: "AMS"},
		3: {ID: 3, SKU: "WASHER-8", Name: "Washer 8mm", Quantity: 3000, Warehouse: "BER"},
	}
	warehouses = []Warehouse{
		{Code: "AMS", City: "Amsterdam"},
		{Code: "BER", City: "Berlin"},
		{Code: "LIS", City: "Lisbon"},
	}
)

// ------------------------------------------------------------------ helpers

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

func detail(w http.ResponseWriter, status int, message string) {
	writeJSON(w, status, map[string]string{"detail": message})
}

// recoverPanics is what every Go web framework ships in some form (gin's
// Recovery, chi's Recoverer): without it, net/http drops the connection on a
// panic and the client sees a network error rather than a 500. The leaked
// stack is the same deliberate choice the other fixtures make - it is the
// signal triage keys on, and a real, common finding on a staging server.
func recoverPanics(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if err := recover(); err != nil {
				writeJSON(w, http.StatusInternalServerError, map[string]string{
					"detail": "Internal Server Error",
					"trace":  fmt.Sprintf("panic: %v\n\n%s", err, debug.Stack()),
				})
			}
		}()
		next.ServeHTTP(w, r)
	})
}

func authenticated(r *http.Request) bool {
	return r.Header.Get("Authorization") == "Bearer "+token
}

func sortedItems() []*Item {
	out := make([]*Item, 0, len(items))
	for _, item := range items {
		out = append(out, item)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out
}

func parseID(w http.ResponseWriter, r *http.Request) (int, bool) {
	id, err := strconv.Atoi(r.PathValue("id"))
	if err != nil {
		detail(w, http.StatusBadRequest, "id must be an integer")
		return 0, false
	}
	return id, true
}

// ----------------------------------------------------------- correct routes

func health(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

// Correct: the optional, typed `limit` is parsed and its error checked. This
// is the false-positive control for case_wrong_query_type - the same shape
// as BUG-502 below, done right.
func listWarehouses(w http.ResponseWriter, r *http.Request) {
	limit := len(warehouses)
	if raw := r.URL.Query().Get("limit"); raw != "" {
		n, err := strconv.Atoi(raw)
		if err != nil || n < 1 {
			detail(w, http.StatusBadRequest, "limit must be a positive integer")
			return
		}
		limit = min(n, len(warehouses))
	}
	writeJSON(w, http.StatusOK, warehouses[:limit])
}

// Correct: the required `code` is checked for presence before it is used.
// The false-positive control for case_missing_required_query - BUG-501's
// shape, done right.
func lookupWarehouse(w http.ResponseWriter, r *http.Request) {
	code := r.URL.Query().Get("code")
	if code == "" {
		detail(w, http.StatusBadRequest, "code is required")
		return
	}
	for _, wh := range warehouses {
		if strings.EqualFold(wh.Code, code) {
			writeJSON(w, http.StatusOK, wh)
			return
		}
	}
	detail(w, http.StatusNotFound, "warehouse not found")
}

// Correct, in the order every handler here should use: authenticate,
// validate the id, check existence, validate the body. The control for every
// body, path and auth rule at once.
func updateStock(w http.ResponseWriter, r *http.Request) {
	if !authenticated(r) {
		detail(w, http.StatusUnauthorized, "missing or invalid bearer token")
		return
	}
	id, ok := parseID(w, r)
	if !ok {
		return
	}
	mu.Lock()
	defer mu.Unlock()
	item, found := items[id]
	if !found {
		detail(w, http.StatusNotFound, "item not found")
		return
	}
	var body struct {
		Quantity *int `json:"quantity"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.Quantity == nil {
		detail(w, http.StatusBadRequest, "quantity is required and must be an integer")
		return
	}
	if *body.Quantity < 0 || *body.Quantity > 100000 {
		detail(w, http.StatusBadRequest, "quantity must be between 0 and 100000")
		return
	}
	item.Quantity = *body.Quantity
	writeJSON(w, http.StatusOK, item)
}

// -------------------------------------------------------------- seeded bugs

// BUG-501: a required query parameter is used without checking it was sent.
//
// Search is by name prefix, bucketed on the first letter. `q[:1]` on an
// empty string is a slice-bounds panic, so a request without `?q=` is a 500
// where 400 is documented. Contrast lookupWarehouse above.
func searchItems(w http.ResponseWriter, r *http.Request) {
	q := strings.ToLower(r.URL.Query().Get("q"))
	bucket := q[:1] // BUG-501: no presence check before slicing
	matches := []*Item{}
	for _, item := range sortedItems() {
		name := strings.ToLower(item.Name)
		if name[:1] == bucket && strings.HasPrefix(name, q) {
			matches = append(matches, item)
		}
	}
	writeJSON(w, http.StatusOK, matches)
}

// BUG-502: an optional typed query parameter is parsed with its error
// discarded.
//
// `strconv.Atoi("abc")` returns (0, err); the error is dropped, page becomes
// 0, and the slice start is -10 - a panic, where 400 is documented. Omitting
// `page` defaults to 1, so the happy path never sees it. Contrast
// listWarehouses above.
func listItems(w http.ResponseWriter, r *http.Request) {
	page := 1
	if raw := r.URL.Query().Get("page"); raw != "" {
		page, _ = strconv.Atoi(raw) // BUG-502: error ignored
	}
	all := sortedItems()
	start := (page - 1) * pageSize
	end := min(start+pageSize, len(all))
	if start >= len(all) {
		writeJSON(w, http.StatusOK, []*Item{})
		return
	}
	writeJSON(w, http.StatusOK, all[start:end])
}

// BUG-503: a map lookup's "not found" is used as if it were a row.
//
// The id is validated correctly (a malformed one is 400). But `items[id]`
// for an absent id is a nil *Item, and reading `.SKU` off it is a nil
// pointer dereference - 500 where 404 is documented. Go's version of
// buggy-shop's BUG-002 and notes-api's BUG-403.
func getItem(w http.ResponseWriter, r *http.Request) {
	id, ok := parseID(w, r)
	if !ok {
		return
	}
	mu.Lock()
	item := items[id]
	mu.Unlock()
	writeJSON(w, http.StatusOK, map[string]any{
		"id":        item.ID, // BUG-503: item may be nil
		"sku":       item.SKU,
		"name":      item.Name,
		"quantity":  item.Quantity,
		"warehouse": item.Warehouse,
	})
}

// BUG-504: an unchecked type assertion on decoded JSON.
//
// The body is decoded into map[string]any, and `body["sku"].(string)` panics
// when 'sku' is absent (nil interface) or not a string (float64 for a JSON
// number). The two-value form `v, ok := ...` exists exactly for this. Go's
// version of the missing-required-field shape every other fixture carries.
func createItem(w http.ResponseWriter, r *http.Request) {
	if !authenticated(r) {
		detail(w, http.StatusUnauthorized, "missing or invalid bearer token")
		return
	}
	var body map[string]any
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		detail(w, http.StatusBadRequest, "body must be a JSON object")
		return
	}
	sku := body["sku"].(string) // BUG-504: unchecked assertion
	name, ok := body["name"].(string)
	if !ok || name == "" {
		detail(w, http.StatusBadRequest, "name is required")
		return
	}
	mu.Lock()
	defer mu.Unlock()
	item := &Item{ID: nextID, SKU: strings.ToUpper(sku), Name: name, Warehouse: "AMS"}
	items[item.ID] = item
	nextID++
	writeJSON(w, http.StatusCreated, item)
}

// ------------------------------------------------------------------ contract

var idParam = map[string]any{
	"name": "id", "in": "path", "required": true, "schema": map[string]any{"type": "integer"},
}

var bearer = []map[string][]string{{"bearerAuth": {}}}

var openAPIDocument = map[string]any{
	"openapi": "3.0.0",
	"info": map[string]any{
		"title":       "Inventory API",
		"version":     "1.0.0",
		"description": "Fixture app for QAgent evaluation. Contains known, labelled defects.",
	},
	"components": map[string]any{
		"securitySchemes": map[string]any{
			"bearerAuth": map[string]any{"type": "http", "scheme": "bearer"},
		},
	},
	"paths": map[string]any{
		"/health": map[string]any{
			"get": map[string]any{"operationId": "health", "responses": map[string]any{"200": map[string]any{}}},
		},
		"/items": map[string]any{
			"get": map[string]any{
				"operationId": "list_items",
				"parameters": []any{map[string]any{
					"name": "page", "in": "query", "required": false,
					"schema": map[string]any{"type": "integer", "minimum": 1},
				}},
				"responses": map[string]any{"200": map[string]any{}, "400": map[string]any{}},
			},
			"post": map[string]any{
				"operationId": "create_item",
				"security":    bearer,
				"requestBody": map[string]any{"content": map[string]any{"application/json": map[string]any{
					"schema": map[string]any{
						"type":     "object",
						"required": []string{"sku", "name"},
						"properties": map[string]any{
							"sku":  map[string]any{"type": "string"},
							"name": map[string]any{"type": "string"},
						},
					},
				}}},
				"responses": map[string]any{"201": map[string]any{}, "400": map[string]any{}, "401": map[string]any{}},
			},
		},
		"/items/search": map[string]any{
			"get": map[string]any{
				"operationId": "search_items",
				"parameters": []any{map[string]any{
					"name": "q", "in": "query", "required": true,
					"schema": map[string]any{"type": "string", "minLength": 1},
				}},
				"responses": map[string]any{"200": map[string]any{}, "400": map[string]any{}},
			},
		},
		"/items/{id}": map[string]any{
			"get": map[string]any{
				"operationId": "get_item",
				"parameters":  []any{idParam},
				"responses":   map[string]any{"200": map[string]any{}, "400": map[string]any{}, "404": map[string]any{}},
			},
		},
		"/items/{id}/stock": map[string]any{
			"put": map[string]any{
				"operationId": "update_stock",
				"security":    bearer,
				"parameters":  []any{idParam},
				"requestBody": map[string]any{"content": map[string]any{"application/json": map[string]any{
					"schema": map[string]any{
						"type":     "object",
						"required": []string{"quantity"},
						"properties": map[string]any{
							"quantity": map[string]any{"type": "integer", "minimum": 0, "maximum": 100000},
						},
					},
				}}},
				"responses": map[string]any{
					"200": map[string]any{}, "400": map[string]any{}, "401": map[string]any{}, "404": map[string]any{},
				},
			},
		},
		"/warehouses": map[string]any{
			"get": map[string]any{
				"operationId": "list_warehouses",
				"parameters": []any{map[string]any{
					"name": "limit", "in": "query", "required": false,
					"schema": map[string]any{"type": "integer", "minimum": 1},
				}},
				"responses": map[string]any{"200": map[string]any{}, "400": map[string]any{}},
			},
		},
		"/warehouses/lookup": map[string]any{
			"get": map[string]any{
				"operationId": "lookup_warehouse",
				"parameters": []any{map[string]any{
					"name": "code", "in": "query", "required": true,
					"schema": map[string]any{"type": "string"},
				}},
				"responses": map[string]any{"200": map[string]any{}, "400": map[string]any{}, "404": map[string]any{}},
			},
		},
	},
}

// -------------------------------------------------------------------- router

func routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", health)
	mux.HandleFunc("GET /openapi.json", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, openAPIDocument)
	})
	mux.HandleFunc("GET /items", listItems)
	mux.HandleFunc("POST /items", createItem)
	mux.HandleFunc("GET /items/search", searchItems)
	mux.HandleFunc("GET /items/{id}", getItem)
	mux.HandleFunc("PUT /items/{id}/stock", updateStock)
	mux.HandleFunc("GET /warehouses", listWarehouses)
	mux.HandleFunc("GET /warehouses/lookup", lookupWarehouse)
	return recoverPanics(mux)
}

func main() {
	log.Printf("inventory-api listening on %s (identities.primary token: %s)", addr, token)
	log.Fatal(http.ListenAndServe(addr, routes()))
}
