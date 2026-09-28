// SPDX-License-Identifier: MPL-2.0
// Run from a TEMPORARY Go module replacing github.com/dusk-network/w3sper-go
// with the unchanged PR #359 checkout at e096a859f55a8e2bb3583bdb44ceed52ba4d7110.
package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"math/big"
	"os"

	"github.com/dusk-network/w3sper-go/base58"
	"github.com/dusk-network/w3sper-go/driver"
	"github.com/dusk-network/w3sper-go/profile"
	"github.com/dusk-network/w3sper-go/tx"
	"github.com/dusk-network/w3sper-go/walletcore"
)

func must(err error) {
	if err != nil {
		panic(err)
	}
}
func decimal(s string) *big.Int {
	n, ok := new(big.Int).SetString(s, 10)
	if !ok {
		panic(s)
	}
	return n
}

func main() {
	ctx := context.Background()
	w, err := walletcore.Load(ctx)
	must(err)
	defer w.Close(ctx)
	wasm, err := os.ReadFile(os.Args[1])
	must(err)
	sum := sha256.Sum256(wasm)
	seed := make([]byte, 64)
	for i := range seed {
		seed[i] = byte(i*7 + 1)
	}
	profiles := []map[string]any{}
	keys := map[int][]byte{}
	for _, index := range []int{0, 1, 255} {
		p, err := w.GenerateProfile(seed, profile.ProfileIndex(index))
		must(err)
		keys[index] = p
		profiles = append(profiles, map[string]any{"index": index, "bytes": hex.EncodeToString(p), "account": base58.Encode(p[64:]), "address": base58.Encode(p[:64])})
	}
	transfers := []map[string]any{}
	for _, tc := range []struct {
		name                                string
		index, receiver, chain              int
		value, deposit, limit, price, nonce string
	}{
		{"standard", 0, 1, 42, "1000000000", "0", "2900000000", "1", "1"},
		{"resolved_nonce", 0, 1, 42, "1000000000", "0", "2900000000", "1", "6"},
		{"upper_bounds", 255, 0, 255, "18446744073709551615", "18446744073709551615", "18446744073709551615", "18446744073709551615", "18446744073709551615"},
		{"self", 0, -1, 0, "0", "0", "0", "0", "0"},
	} {
		var receiver []byte
		if tc.receiver >= 0 {
			receiver = keys[tc.receiver][64:]
		}
		buf, hash, err := w.Moonlight(driver.MoonlightInput{Seed: seed, SenderIndex: profile.ProfileIndex(tc.index), Receiver: receiver, TransferValue: decimal(tc.value), Deposit: decimal(tc.deposit), GasLimit: decimal(tc.limit), GasPrice: decimal(tc.price), Nonce: decimal(tc.nonce), ChainID: tc.chain})
		must(err)
		transfers = append(transfers, map[string]any{"name": tc.name, "sender_index": tc.index, "receiver_index": tc.receiver, "chain_id": tc.chain, "value": tc.value, "deposit": tc.deposit, "gas_limit": tc.limit, "gas_price": tc.price, "nonce": tc.nonce, "bytes": hex.EncodeToString(buf), "hash": hash})
	}
	// Independently exercise the pinned Go high-level nonce increment.
	gen := profile.NewGenerator(func() ([]byte, error) { return seed, nil }, w)
	sender, err := gen.Default()
	must(err)
	route, err := tx.NewTransfer(tx.NewBookEntry(sender, nil, w)).Amount(big.NewInt(1000000000)).Chain(42).Nonce(big.NewInt(5)).To(base58.Encode(keys[1][64:]))
	must(err)
	built, err := route.Account.Build()
	must(err)
	if hex.EncodeToString(built.Buffer) != transfers[1]["bytes"] || built.Hash != transfers[1]["hash"] || built.Nonce.String() != "6" {
		panic("Go builder mismatch")
	}
	_, _, err = w.Moonlight(driver.MoonlightInput{Seed: seed, Receiver: make([]byte, 96), TransferValue: big.NewInt(1), Deposit: big.NewInt(0), GasLimit: big.NewInt(2900000000), GasPrice: big.NewInt(1), Nonce: big.NewInt(1), ChainID: 42})
	var code *walletcore.CodeError
	if !errors.As(err, &code) {
		panic("expected wallet-core CodeError for invalid account")
	}
	g := w.Globals()
	out := map[string]any{
		"provenance": map[string]any{"repository": "dusk-network/rusk-private", "pr": 359, "commit": "e096a859f55a8e2bb3583bdb44ceed52ba4d7110", "go": "go1.27.1 linux/amd64", "wasm_version": walletcore.Version, "wasm_sha256": hex.EncodeToString(sum[:])},
		"seed":       hex.EncodeToString(seed), "profiles": profiles, "transfers": transfers,
		"globals":                map[string]any{"key_size": g.KeySize, "item_size": g.ItemSize, "minimum_stake": g.MinStake, "heap_base": g.HeapBase},
		"invalid_account_status": code.Code,
	}
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	must(enc.Encode(out))
}
