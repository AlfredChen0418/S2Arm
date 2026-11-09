#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 S2Arm contributors
"""Independent expectations for the T221 add / min-max cases (integer/add_*.json).

A small interpreter over the cases' readable instruction text, from ISA semantics, all results mod 2^32:
IADD: A' + B', where '-' is two's-complement negation of that source (at most one of -A / -B / .PO);
IADD3: A' + B' + C'; ISCADD: (A' << shift) + B'; ISCADD32I: (A << shift) + imm32;
.PO (both negate bits, the decoder's AvgMode.PlusOne) adds one with neither source negated, as ruled in #1339
(nvdisasm prints IADD.PO without negation marks; Mesa NAK sm50.rs encodes 'both negated' as iadd.po):
IADD.PO = A + B + 1, ISCADD.PO = (A << shift) + B + 1; a '-' together with .PO is outside the modelled domain;
IADD.PO.CC splits the full unsigned sum into CF and the low word; IADD.X adds the previous CF.
IMNMX.<U32|S32>.<MIN|MAX>: the smaller / larger of A and B compared unsigned or signed.
Immediates of the 20-bit forms are signed 20-bit; c[0][offset] reads the framework's zero-filled C0.
MOV32I sets a register. It imports no encoder, translator, SPIR-V or produced result.
Usage: python3 tests/shader_cases/integer/add_reference_test.py
"""
import copy
import json
from pathlib import Path
import re

M = 0xffffffff


def signed(v):
    return v - (1 << 32) if v >> 31 else v


def execute(instructions, initial):
    if len(initial) != 64 or any(type(v) is not int or not 0 <= v <= M for v in initial):
        raise ValueError("reference domain is exactly 64 uint32 words")
    memory = list(initial)
    registers = {255: 0}
    carry = None

    def number(token):
        if token == "RZ":
            return 255
        if not re.fullmatch(r"R\d+", token) or not 0 <= int(token[1:]) < 255:
            raise ValueError("bad register " + token)
        return int(token[1:])

    def read(token):
        n = number(token)
        if n not in registers:
            raise ValueError("undefined reference register " + token)
        return registers[n]

    def write(token, value):
        if number(token) != 255:
            registers[number(token)] = value & M

    def source(token, register_only=False):
        negate = token[:1] == "-" and token[1:2] in ("R", "c")
        token = token[1:] if negate else token
        if token.startswith("R"):
            v = read(token)
        elif register_only:
            raise ValueError("register operand expected: " + token)
        elif re.fullmatch(r"c\[0\]\[\w+\]", token):
            v = 0  # framework C0: 64 KiB of zeros
        else:
            v = int(token, 0)
            if not -(1 << 19) <= v < 1 << 19:
                raise ValueError("20-bit immediate out of range: " + token)
            v &= M
        return (-v if negate else v) & M, negate

    def word(base, offset):
        if read(base) or read("R" + str(number(base) + 1)):
            raise ValueError("reference uses the zero C0 base address only")
        pos = int(offset, 0)
        if pos % 4 or not 0 <= pos <= 252:
            raise ValueError("reference uses only aligned, in-range uint32 access")
        return pos // 4

    for index, line in enumerate(instructions):
        op, *a = line.split()
        m = op.split(".")
        if op == "LDC64" and a[1:] == ["0", "0"] and number(a[0]) < 254:
            write(a[0], 0)
            write("R" + str(number(a[0]) + 1), 0)
        elif op == "LDG32" and len(a) == 3:
            write(a[0], memory[word(a[1], a[2])])
        elif op == "STG32" and len(a) == 3:
            memory[word(a[1], a[2])] = read(a[0])
        elif op == "MOV32I" and len(a) == 2:
            write(a[0], int(a[1], 0))
        elif op == "IADD.PO.CC" and len(a) == 3:
            carry, low = divmod(read(a[1]) + read(a[2]) + 1, 1 << 32)
            write(a[0], low)
        elif op == "IADD.X" and len(a) == 3:
            if carry is None:
                raise ValueError("IADD.X requires a preceding carry producer: " + line)
            write(a[0], read(a[1]) + read(a[2]) + carry)
        elif m[0] == "IADD" and set(m[1:]) <= {"PO"} and len(a) == 3:
            (x, nx), (y, ny) = source(a[1], True), source(a[2])
            if nx + ny + ("PO" in m) > 1:
                raise ValueError("IADD takes at most one of -A / -B / .PO: " + line)
            write(a[0], x + y + ("PO" in m))
        elif op == "IADD3" and len(a) == 4:
            write(a[0], source(a[1], True)[0] + source(a[2])[0] + source(a[3], True)[0])
        elif m[0] == "ISCADD" and set(m[1:]) <= {"PO"} and len(a) == 4 and 0 <= int(a[3], 0) < 32:
            (x, nx), (y, ny) = source(a[1], True), source(a[2])
            if nx + ny + ("PO" in m) > 1:
                raise ValueError("ISCADD takes at most one of -A / -B / .PO: " + line)
            write(a[0], (x << int(a[3], 0)) + y + ("PO" in m))
        elif op == "ISCADD32I" and len(a) == 4 and 0 <= int(a[3], 0) < 32 and a[1].startswith("R"):
            write(a[0], (read(a[1]) << int(a[3], 0)) + (int(a[2], 0) & M))
        elif m[0] == "IMNMX" and len(m) == 3 and m[1] in ("U32", "S32") and m[2] in ("MIN", "MAX") and len(a) == 3:
            x, y = source(a[1], True)[0], source(a[2])[0]
            key = signed if m[1] == "S32" else (lambda v: v)
            write(a[0], (min if m[2] == "MIN" else max)(x, y, key=key))
        elif op == "EXIT" and not a and index == len(instructions) - 1:
            return memory
        else:
            raise ValueError("outside reference instruction domain: " + line)
    raise ValueError("explicit final EXIT required")


def verify(case):
    if case["stage"] != "compute" or set(case["inputs"]) != {"storage0"}:
        raise ValueError("reference is single-invocation storage0 only")
    actual = execute(case["instructions"], case["inputs"]["storage0"])
    if actual != case["expected"]:
        raise AssertionError("independent expectation mismatch: " + case["id"])
    return actual


def main():
    # Hand-computed anchors, so a slip in the interpreter itself cannot pass silently.
    run = lambda lines, regs: execute(["LDC64 R0 0 0"] + [f"MOV32I R{r} {v:#x}" for r, v in regs.items()] + lines + ["STG32 R9 R0 0", "EXIT"], [0] * 64)[0]
    assert run(["IADD R9 R2 R3"], {2: M, 3: 1}) == 0 and run(["IADD R9 R2 R3"], {2: 0x7fffffff, 3: 1}) == 0x80000000
    assert run(["IADD R9 -R2 R3"], {2: 5, 3: 3}) == 0xfffffffe and run(["IADD R9 R2 -R3"], {2: 3, 3: 5}) == 0xfffffffe
    assert run(["IADD3 R9 R2 -R3 R4"], {2: 10, 3: 3, 4: 1}) == 8 and run(["ISCADD R9 R2 R3 4"], {2: 0x12345678, 3: 1}) == 0x23456781
    assert run(["ISCADD R9 -R2 R3 1"], {2: 1, 3: 0}) == 0xfffffffe and run(["ISCADD32I R9 R2 0x80000000 31"], {2: 1}) == 0
    assert run(["IMNMX.S32.MIN R9 R2 R3"], {2: 0x80000001, 3: 1}) == 0x80000001 and run(["IMNMX.U32.MIN R9 R2 R3"], {2: 0x80000001, 3: 1}) == 1
    assert run(["IMNMX.S32.MAX R9 R2 -1"], {2: 0}) == 0 and run(["IMNMX.U32.MAX R9 R2 -1"], {2: 0}) == M
    assert run(["IADD.PO R9 R2 R3"], {2: 0x12345678, 3: 0xdeadbeef}) == 0xf0e21568 and run(["IADD.PO R9 R2 R3"], {2: 0, 3: M}) == 0
    assert run(["ISCADD.PO R9 R2 R3 4"], {2: 1, 3: 0xffffffef}) == 0 and run(["ISCADD.PO R9 R2 R3 0"], {2: 1, 3: M}) == 1
    paths = sorted(Path(__file__).parent.glob("add_*.json"))
    if not paths:
        raise ValueError("no original integer add / min-max cases")
    for path in paths:
        case = json.loads(path.read_text())
        actual = verify(case)
        changed = [i for i, (x, y) in enumerate(zip(actual, case["inputs"]["storage0"])) if x != y]
        for i in changed:
            bad = copy.deepcopy(case)
            bad["expected"][i] ^= 1
            try:
                verify(bad)
            except AssertionError:
                pass
            else:
                raise AssertionError("wrong expectation survived: %s word %d" % (case["id"], i))
        print("reference %s: 64 words match; %d result words, each one-bit wrong expectation rejected" % (case["id"], len(changed)))
    for bad_line in ("IADD.PO R3 -R2 R2", "ISCADD.PO R3 R2 -R2 1", "IADD R3 -R2 -R2", "ISCADD R3 R2 R2 32", "IMNMX.S32 R3 R2 R2", "IADD3 R3 R2 R2 5"):
        try:
            execute(["LDC64 R0 0 0", "MOV32I R2 1", bad_line, "EXIT"], [0] * 64)
        except ValueError:
            pass
        else:
            raise AssertionError("instruction outside the modelled domain accepted: " + bad_line)
    print("reference: %d add / min-max cases; out-of-domain forms rejected" % len(paths))


if __name__ == "__main__":
    main()
