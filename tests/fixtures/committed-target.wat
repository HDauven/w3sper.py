;; SPDX-License-Identifier: MPL-2.0
;; Minimal stateful host-backed-ABI contract for the parity lab.
;; ABI follows the pinned rusk/vm/tests/committed_call.rs::host_backed_target.
;; init/run accept exactly 32 raw bytes and persist them; value returns them.
(module
  (import "env" "__piecrust_b_call_input_copy" (func $input (param i32 i32 i32)))
  (import "env" "__piecrust_b_call_output_set" (func $output (param i32 i32)))
  (memory (export "memory") 1 1)
  (global (export "B") i32 (i32.const 0))
  (func $run (export "run") (export "init") (param $len i32) (result i32)
    local.get $len
    i32.const 32
    i32.ne
    if unreachable end
    i32.const 64
    i32.const 0
    i32.const 32
    call $input
    i32.const 64
    i32.const 32
    call $output
    i32.const 32)
  (func (export "value") (param i32) (result i32)
    i32.const 64
    i32.const 32
    call $output
    i32.const 32))
