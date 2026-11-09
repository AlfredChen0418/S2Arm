#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 S2Arm contributors
"""Original synthetic Maxwell cases, production translator, real CPU Vulkan."""
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import time
from coverage import IADD_PO_CASE, IADD_PO_PATCH, collect_coverage

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
EXIT=0xe30000000007000f
IADD_PO_CARRY_CASE = 'integer/add_po_carry'
IADD_PO_CARRY_MUTATION = 'S2A_TEST_IADD_PO_CARRY_MUTATION'

def register(s):
    if s=='RZ': return 255
    if not s.startswith('R') or not 0<=int(s[1:])<255: raise ValueError(s)
    return int(s[1:])

# T221 integer logic (Decoders/InstTable.cs + InstDecoders.cs): '~' before a source sets its invert bit; the second
# source is Rn, a signed 20-bit immediate (low 19 bits at 38:20, sign at 56) or c[slot][byte offset] (word units at 33:20).
LOGIC={'AND':0,'OR':1,'XOR':2,'PASS_B':3}

def inverted(s): return (1,s[1:]) if s.startswith('~') else (0,s)

def source_b(s):
    if s.startswith('R'): return 'r',register(s)<<20
    m=re.fullmatch(r'c\[(\w+)\]\[(\w+)\]',s)
    if m:
        slot,off=int(m.group(1),0),int(m.group(2),0); assert 0<=slot<32 and 0<=off<0x10000 and off%4==0
        return 'c',(slot<<34)|((off>>2)<<20)
    v=int(s,0)
    if not -0x80000<=v<0x80000: raise ValueError('20-bit signed immediate out of range: '+s)
    return 'i',((v&0x7ffff)<<20)|((v<0)<<56)

SCALE={'D2':1,'D4':2,'D8':3,'M8':4,'M4':5,'M2':6}

def f32bits(s): return struct.unpack('<I',struct.pack('<f',float(s)))[0]

def float_mods(s):
    neg=s[:1]=='-' and s[1:2] in ('R','|','c')
    if neg: s=s[1:]
    absolute=len(s)>2 and s[0]=='|' and s[-1]=='|'
    return neg,absolute,(s[1:-1] if absolute else s)

def float_b(s):
    if s.startswith(('R','c[')): return source_b(s)
    v=f32bits(s)
    if v&0xfff: raise ValueError('20-bit float immediate needs the low 12 bits zero: '+s)
    return 'i',(((v>>12)&0x7ffff)<<20)|((v>>31)<<56)

def encode(line):
    a=line.replace(',',' ').split(); op=a.pop(0)
    if op=='EXIT' and not a: return EXIT
    if op.startswith('LOP.') and op[4:] in LOGIC and len(a)==3:
        na,ra=inverted(a[1]); nb,b=inverted(a[2]); kind,bits=source_b(b)
        return {'r':0x5c40000000000000,'i':0x3840000000000000,'c':0x4c40000000000000}[kind]|bits|(7<<48)|(LOGIC[op[4:]]<<41)|(nb<<40)|(na<<39)|(7<<16)|(register(ra)<<8)|register(a[0])
    if op.startswith('LOP32I.') and op[7:] in LOGIC and len(a)==3:
        na,ra=inverted(a[1]); nb,b=inverted(a[2]); v=int(b,0); assert -0x80000000<=v<=0xffffffff
        return 0x0400000000000000|(nb<<56)|(na<<55)|(LOGIC[op[7:]]<<53)|((v&0xffffffff)<<20)|(7<<16)|(register(ra)<<8)|register(a[0])
    if op=='LOP3.LUT' and len(a)==5:
        kind,bits=source_b(a[2]); lut=int(a[4],0); assert 0<=lut<256
        if kind=='r': return 0x5be0000000000000|(7<<48)|(register(a[3])<<39)|(lut<<28)|bits|(7<<16)|(register(a[1])<<8)|register(a[0])
        return {'i':0x3c00000000000000,'c':0x0200000000000000}[kind]|(lut<<48)|(register(a[3])<<39)|bits|(7<<16)|(register(a[1])<<8)|register(a[0])
    # T221 shifts: SHL[.W], SHR.<U32|S32>[.W][.BREV] take B like LOP; SHF.<L|R>[.U64|.S64][.W] Rd Ra Rb|imm6 Rc (.X / .CC / .HI not modelled).
    m=op.split('.')
    if m[0]=='SHL' and set(m[1:])<={'W'} and len(a)==3:
        kind,bits=source_b(a[2])
        return {'r':0x5c48000000000000,'i':0x3848000000000000,'c':0x4c48000000000000}[kind]|bits|(('W' in m)<<39)|(7<<16)|(register(a[1])<<8)|register(a[0])
    if m[0]=='SHR' and len(m)>1 and m[1] in ('U32','S32') and set(m[2:])<={'W','BREV'} and len(a)==3:
        kind,bits=source_b(a[2])
        return {'r':0x5c28000000000000,'i':0x3828000000000000,'c':0x4c28000000000000}[kind]|bits|((m[1]=='S32')<<48)|(('BREV' in m)<<40)|(('W' in m)<<39)|(7<<16)|(register(a[1])<<8)|register(a[0])
    if m[0]=='SHF' and len(m)>1 and m[1] in ('L','R') and set(m[2:])<={'W','U64','S64'} and not {'U64','S64'}<=set(m) and len(a)==4:
        common=(('W' in m)<<50)|(register(a[3])<<39)|({'U64':2,'S64':3}.get(next((x for x in m if x in ('U64','S64')),''),0)<<37)|(7<<16)|(register(a[1])<<8)|register(a[0])
        if a[2].startswith('R'): return (0x5bf8000000000000 if m[1]=='L' else 0x5cf8000000000000)|common|(register(a[2])<<20)
        n=int(a[2],0); assert 0<=n<64
        return (0x36f8000000000000 if m[1]=='L' else 0x38f8000000000000)|common|(n<<20)
    # T221 bitfield: BFE.<U32|S32>[.BREV] Rd Ra B, BFI Rd Ra B Rc|c[][] (a c[][] base is the Rc form, B then a register),
    # POPC Rd [~]B, FLO.<U32|S32>[.SH] Rd [~]B; B = position | length << 8 for BFE / BFI (.CC / .X not modelled).
    if m[0]=='BFE' and len(m)>1 and m[1] in ('U32','S32') and set(m[2:])<={'BREV'} and len(a)==3:
        kind,bits=source_b(a[2])
        return {'r':0x5c00000000000000,'i':0x3800000000000000,'c':0x4c00000000000000}[kind]|bits|((m[1]=='S32')<<48)|(('BREV' in m)<<40)|(7<<16)|(register(a[1])<<8)|register(a[0])
    if op=='BFI' and len(a)==4:
        if a[3].startswith('c['):
            return 0x53f0000000000000|source_b(a[3])[1]|(register(a[2])<<39)|(7<<16)|(register(a[1])<<8)|register(a[0])
        kind,bits=source_b(a[2])
        return {'r':0x5bf0000000000000,'i':0x36f0000000000000,'c':0x4bf0000000000000}[kind]|bits|(register(a[3])<<39)|(7<<16)|(register(a[1])<<8)|register(a[0])
    if (op=='POPC' or m[0]=='FLO' and len(m)>1 and m[1] in ('U32','S32') and set(m[2:])<={'SH'}) and len(a)==2:
        nb,b=inverted(a[1]); kind,bits=source_b(b)
        base={'r':0x5c08000000000000,'i':0x3808000000000000,'c':0x4c08000000000000} if op=='POPC' else {'r':0x5c30000000000000,'i':0x3830000000000000,'c':0x4c30000000000000}
        return base[kind]|bits|((m[0]=='FLO' and m[1]=='S32')<<48)|(('SH' in m)<<41)|(nb<<40)|(7<<16)|(255<<8)|register(a[0])
    # T221 add / min-max: IADD[.PO] Rd [-]Ra [-]B (one of -A / -B / .PO), IADD3 Rd [-]Ra [-]B [-]Rc, ISCADD[.PO] Rd [-]Ra [-]B shift,
    # ISCADD32I Rd Ra imm32 shift, IMNMX.<U32|S32>.<MIN|MAX> Rd Ra B; '-' on a register or c[][] is two's-complement negation
    # IADD.PO.CC and IADD.X are restricted to the non-negated register forms used to observe carry.
    # .SAT and IADD3 half selects / shifts are outside this subset.
    neg=lambda s: (1,s[1:]) if s[:1]=='-' and s[1:2] in ('R','c') else (0,s)
    carry_form = op in ('IADD.PO.CC', 'IADD.X')
    if m[0] in ('IADD','ISCADD') and (set(m[1:]) <= {'PO'} or carry_form) and len(a)==(3 if m[0]=='IADD' else 4):
        na,ra=neg(a[1]); nb,b=neg(a[2]); assert na+nb+('PO' in m)<=1, 'one of -A / -B / .PO'
        kind,bits=source_b(b); mode=3 if 'PO' in m else 2 if na else 1 if nb else 0
        if m[0]=='IADD':
            if carry_form and (kind != 'r' or na or nb):
                raise ValueError('IADD.PO.CC and IADD.X require non-negated registers: ' + line)
            flags = (('CC' in m) << 47) | (('X' in m) << 43)  # InstIaddR.WriteCC and X.
            base = {'r': 0x5c10000000000000, 'i': 0x3810000000000000, 'c': 0x4c10000000000000}[kind]
            return base | bits | (mode << 48) | flags | (7 << 16) | (register(ra) << 8) | register(a[0])
        s=int(a[3],0); assert 0<=s<32
        return {'r':0x5c18000000000000,'i':0x3818000000000000,'c':0x4c18000000000000}[kind]|bits|(mode<<48)|(s<<39)|(7<<16)|(register(ra)<<8)|register(a[0])
    if op=='ISCADD32I' and len(a)==4:
        s=int(a[3],0); assert 0<=s<32
        return 0x1400000000000000|(s<<53)|((int(a[2],0)&0xffffffff)<<20)|(7<<16)|(register(a[1])<<8)|register(a[0])
    if op=='IADD3' and len(a)==4:
        na,ra=neg(a[1]); nb,b=neg(a[2]); nc,rc=neg(a[3]); kind,bits=source_b(b)
        return {'r':0x5cc0000000000000,'i':0x38c0000000000000,'c':0x4cc0000000000000}[kind]|bits|(na<<51)|(nb<<50)|(nc<<49)|(register(rc)<<39)|(7<<16)|(register(ra)<<8)|register(a[0])
    if m[0]=='IMNMX' and len(m)==3 and m[1] in ('U32','S32') and m[2] in ('MIN','MAX') and len(a)==3:
        kind,bits=source_b(a[2])
        return {'r':0x5c20000000000000,'i':0x3820000000000000,'c':0x4c20000000000000}[kind]|bits|((m[1]=='S32')<<48)|((m[2]=='MAX')<<42)|(7<<39)|(7<<16)|(register(a[1])<<8)|register(a[0])
    # T221 IMUL.<U32|S32>.<U32|S32> Rd Ra B: low word only; no HI/CC.
    # Independent encoding: envytools gm107.c f102b82381f3f11cee113d16374c87091db039d9,
    # lines 1371-1379 (signed bits 40/41), 2010/2084/2130 (R/C/I). C is limited to the existing zero C0.
    if m[0]=='IMUL' and len(m)==3 and all(x in ('U32','S32') for x in m[1:]) and len(a)==3:
        kind,bits=source_b(a[2])
        if kind=='c' and bits&(31<<34): raise ValueError('IMUL supports only the existing zero C0')
        return {'r':0x5c38000000000000,'i':0x3838000000000000,'c':0x4c38000000000000}[kind]|bits|((m[1]=='S32')<<40)|((m[2]=='S32')<<41)|(7<<16)|(register(a[1])<<8)|register(a[0])
    # T221 XMAD[.<U16|S16>.<U16|S16>][.CLO|.CHI|.CBCC][.PSL][.MRG] Rd Ra[.H1] B C (16 x 16 multiply-add; .CSFU / .X / .CC not modelled).
    # The form follows the operands: B register + C register = R, B 16-bit immediate = I, B c[][] = C, C c[][] (B a register) = Rc.
    # Independent encoding: envytools gm107.c f102b82381f3f11cee113d16374c87091db039d9, lines 1457 (signed A / B bits 48 / 49),
    # 1465 / 1580 (3-bit / 2-bit C mode at 50), 2031 / 2149 / 2064 / 2050 (R / I / C / Rc: .H1, .PSL, .MRG positions per form).
    if m[0]=='XMAD' and len(a)==4:
        f=m[1:]; sg=f[:2] if f[:2] and f[0] in ('U16','S16') else []
        if sg and (len(sg)<2 or sg[1] not in ('U16','S16')): raise ValueError('XMAD takes both A and B signedness or neither: '+line)
        rest=f[len(sg):]; cop=[x for x in rest if x in ('CLO','CHI','CBCC')]
        if len(cop)>1 or set(rest)-{'CLO','CHI','CBCC','PSL','MRG'} or len(set(rest))!=len(rest): raise ValueError('bad XMAD modifiers: '+line)
        cop={'CLO':1,'CHI':2,'CBCC':4}[cop[0]] if cop else 0; psl,mrg='PSL' in rest,'MRG' in rest
        sa,sb=(sg[0]=='S16',sg[1]=='S16') if sg else (False,False)
        half=lambda x: (1,x[:-3]) if x.endswith('.H1') else (0,x)
        ha,ra=half(a[1]); hb,b=half(a[2]); c=a[3]
        common=(sa<<48)|(sb<<49)|(ha<<53)|(7<<16)|(register(ra)<<8)|register(a[0])
        if c.startswith('c['):
            if not b.startswith('R') or psl or mrg or cop>2: raise ValueError('XMAD Rc form: B register, 2-bit C mode, no .PSL / .MRG: '+line)
            return 0x5100000000000000|common|source_b(c)[1]|(register(b)<<39)|(cop<<50)|(hb<<52)
        if b.startswith('c['):
            if cop>2: raise ValueError('XMAD C form has a 2-bit C mode (no .CBCC): '+line)
            return 0x4e00000000000000|common|source_b(b)[1]|(register(c)<<39)|(cop<<50)|(hb<<52)|(psl<<55)|(mrg<<56)
        if b.startswith('R'):
            return 0x5b00000000000000|common|(register(b)<<20)|(hb<<35)|(psl<<36)|(mrg<<37)|(register(c)<<39)|(cop<<50)
        v=int(b,0)
        if hb or not 0<=v<=0xffff: raise ValueError('XMAD I form takes an unsigned 16-bit immediate, no .H1: '+line)
        return 0x3600000000000000|common|(v<<20)|(psl<<36)|(mrg<<37)|(register(c)<<39)|(cop<<50)
    if op=='MOV32I' and len(a)==2:
        return 0x0100000000000000|((int(a[1],0)&0xffffffff)<<20)|(7<<16)|(15<<12)|register(a[0])
    if op in ('IADD32I','FADD32I') and len(a)==3:
        value=struct.unpack('<I',struct.pack('<f',float(a[2])))[0] if op=='FADD32I' else int(a[2],0)&0xffffffff
        return (0x0800000000000000 if op=='FADD32I' else 0x1c00000000000000)|(value<<20)|(7<<16)|(register(a[1])<<8)|register(a[0])
    # T222 FADD[.SAT] Rd A B, FMUL[.SAT][.D2|.D4|.D8|.M2|.M4|.M8] Rd [-]Ra B, FMUL32I[.SAT] Rd Ra float (round-to-nearest only):
    # '-' / '|x|' before a register or c[][] set the negate / absolute bits; a 20-bit float immediate is the float's top 20 bits.
    f=op.split('.')
    if f[0] in ('FADD','FMUL') and len(a)==3:
        na,aa,ra=float_mods(a[1]); nb,ab,b=float_mods(a[2]); kind,bits=float_b(b); sat=('SAT' in f)<<50
        if f[0]=='FADD' and set(f[1:])<={'SAT'}:
            return {'r':0x5c58000000000000,'i':0x3858000000000000,'c':0x4c58000000000000}[kind]|bits|sat|(ab<<49)|(na<<48)|(aa<<46)|(nb<<45)|(7<<16)|(register(ra)<<8)|register(a[0])
        scale=[SCALE[x] for x in f[1:] if x in SCALE]
        if f[0]=='FMUL' and set(f[1:])<={'SAT'}|set(SCALE) and len(scale)<=1 and not (aa or nb or ab):
            return {'r':0x5c68000000000000,'i':0x3868000000000000,'c':0x4c68000000000000}[kind]|bits|sat|(na<<48)|((scale or [0])[0]<<41)|(7<<16)|(register(ra)<<8)|register(a[0])
    if f[0]=='FMUL32I' and set(f[1:])<={'SAT'} and len(a)==3:
        return 0x1e00000000000000|(('SAT' in f)<<55)|(f32bits(a[2])<<20)|(7<<16)|(register(a[1])<<8)|register(a[0])
    # T222 FFMA[.SAT] Rd Ra [-]B [-]C (C = Rn or c[][]; a c[][] C with a register B is the Rc form), FFMA32I[.SAT] Rd Ra [-]float [-]Rd
    # (C is Rd itself), FMNMX.<MIN|MAX> Rd A B (MIN = predicate PT, MAX = !PT; '-' / '|x|' on both sources).
    if f[0]=='FFMA' and set(f[1:])<={'SAT'} and len(a)==4:
        nb,_,b=float_mods(a[2]); nc,_,c=float_mods(a[3]); sat=('SAT' in f)<<50
        common=sat|(nc<<49)|(nb<<48)|(7<<16)|(register(a[1])<<8)|register(a[0])
        if c.startswith('c['): return 0x5180000000000000|source_b(c)[1]|(register(b)<<39)|common
        kind,bits=float_b(b)
        return {'r':0x5980000000000000,'i':0x3280000000000000,'c':0x4980000000000000}[kind]|bits|(register(c)<<39)|common
    if f[0]=='FFMA32I' and set(f[1:])<={'SAT'} and len(a)==4 and a[3].lstrip('-')==a[0]:
        nb=a[2].startswith('-'); v=f32bits(a[2].lstrip('-'))
        return 0x0c00000000000000|(a[3].startswith('-')<<57)|(nb<<56)|(('SAT' in f)<<55)|(v<<20)|(7<<16)|(register(a[1])<<8)|register(a[0])
    if f[0]=='FMNMX' and len(f)==2 and f[1] in ('MIN','MAX') and len(a)==3:
        na,aa,ra=float_mods(a[1]); nb,ab,b=float_mods(a[2]); kind,bits=float_b(b)
        return {'r':0x5c60000000000000,'i':0x3860000000000000,'c':0x4c60000000000000}[kind]|bits|(ab<<49)|(na<<48)|(aa<<46)|(nb<<45)|((f[1]=='MAX')<<42)|(7<<39)|(7<<16)|(register(ra)<<8)|register(a[0])
    # T222 FSET.<LT|EQ|LE|GT|NE|GE>[.BF] Rd A B; fixed AND PT, R/I only.
    # Independent encoding: envytools gm107.c f102b82381f3f11cee113d16374c87091db039d9,
    # comparison/logic tables 1410-1434, R/I rows 2036/2154; no FTZ/CC/Cbuf.
    if f[0]=='FSET' and len(f) in (2,3) and f[1] in ('LT','EQ','LE','GT','NE','GE') and (len(f)==2 or f[2]=='BF') and len(a)==3:
        na,aa,ra=float_mods(a[1]); nb,ab,b=float_mods(a[2]); kind,bits=float_b(b)
        if kind not in ('r','i'): raise ValueError('FSET supports R/I only')
        comparison={'LT':1,'EQ':2,'LE':3,'GT':4,'NE':5,'GE':6}[f[1]]
        return {'r':0x5800000000000000,'i':0x3000000000000000}[kind]|bits|(aa<<54)|(nb<<53)|(('BF' in f)<<52)|(comparison<<48)|(ab<<44)|(na<<43)|(7<<39)|(7<<16)|(register(ra)<<8)|register(a[0])
    # I2F RN only; independent envytools gm107.c f102b823 table 1256-1281.
    if op in ('I2F.F32.S32','I2F.F32.U32') and len(a)==2:
        kind,bits=source_b(a[1])
        if kind not in ('r','i'): raise ValueError('I2F supports R/I only')
        fmt=0x2a00 if op.endswith('S32') else 0x0a00
        return {'r':0x5cb8000000000000,'i':0x38b8000000000000}[kind]|bits|fmt|(7<<16)|register(a[0])
    # F2I F32 -> S32/U32, RN/RZ; envytools gm107.c f102b823 tables 1283-1307, rows 1996/2116.
    if len(f)==4 and f[0]=='F2I' and f[1] in ('S32','U32') and f[2]=='F32' and f[3] in ('RN','RZ') and len(a)==2:
        kind,bits=float_b(a[1])
        if kind not in ('r','i'): raise ValueError('F2I supports R/I only')
        if kind=='i':
            from fractions import Fraction
            if Fraction(a[1]) != Fraction(struct.unpack('<f',struct.pack('<I',f32bits(a[1])))[0]):
                raise ValueError('F2I immediate must be exactly representable binary32')
        fmt=0x1a00 if f[1]=='S32' else 0x0a00
        return {'r':0x5cb0000000000000,'i':0x38b0000000000000}[kind]|bits|fmt|((3 if f[3]=='RZ' else 0)<<39)|(7<<16)|register(a[0])
    if op=='ALD' and len(a)==3:
        loc,comp=int(a[1]),int(a[2]); assert 0<=loc<32 and 0<=comp<4
        return 0xefd8000000000000|(255<<39)|((0x80+loc*16+comp*4)<<20)|(7<<16)|(255<<8)|register(a[0])
    if op=='LDC64' and len(a)==3:
        slot,off=int(a[1],0),int(a[2],0); assert 0<=slot<16 and 0<=off<65536
        return 0xef95000000000000|(slot<<36)|(off<<20)|(7<<16)|(255<<8)|register(a[0])
    if op in ('LDG32','STG32') and len(a)==3:
        off=int(a[2],0); assert 0<=off<1<<24
        return (0xeed4200000000000 if op=='LDG32' else 0xeedc200000000000)|(off<<20)|(7<<16)|(register(a[1])<<8)|register(a[0])
    raise ValueError('unsupported instruction: '+line)

def compare(case_id, expected, actual):
    if expected!=actual: raise AssertionError(f"{case_id}: expected {expected} -> actual {actual}")

def expect_mismatch(case_id, expected, actual):
    try: compare(case_id,expected,actual)
    except AssertionError as error: print('NEGATIVE rejected: '+str(error),flush=True)
    else: raise AssertionError('negative control survived: '+case_id)

def check_device_capabilities(data):
    # This harness's vk_device.c does not enable fragment shader interlock.
    # SPIR-V enums are the Khronos values in Spv.Generator/spirv.cs.
    if len(data)<20 or len(data)%4: raise ValueError('invalid SPIR-V size')
    words=struct.unpack('<'+'I'*(len(data)//4),data)
    if words[0]!=0x07230203: raise ValueError('invalid SPIR-V magic')
    offset=5
    while offset<len(words):
        size,op=words[offset]>>16,words[offset]&0xffff
        if not size or offset+size>len(words): raise ValueError('invalid SPIR-V instruction size')
        args=words[offset+1:offset+size]
        if op==17 and args and args[0] in (5363,5372,5378):
            raise AssertionError('SPIR-V interlock capability not enabled on test logical device')
        if op==10:
            extension=data[(offset+1)*4:(offset+size)*4].split(b'\0',1)[0]
            if extension==b'SPV_EXT_fragment_shader_interlock':
                raise AssertionError('SPIR-V interlock extension not enabled on test logical device')
        if op in (5364,5365):
            raise AssertionError('interlock instruction outside fixed case device contract')
        offset+=size

def binary(case):
    allowed={'varying0'} if case['stage']=='fragment' else {'storage0'}
    if set(case.get('inputs',{}))-allowed: raise ValueError('unsupported input field')
    if case.get('schema')!=1: raise ValueError('unsupported case schema')
    if case['stage'] not in ('fragment','compute'): raise ValueError('unsupported stage')
    if case['stage']=='fragment':
        if case.get('inputs',{}).get('varying0',[0.25,0.5,0.75,1]) != [0.25,0.5,0.75,1]:
            raise ValueError('v1 varying0 must match fixed independent vertex fixture')
        if len(case['expected'])!=4 or any(type(v)!=int or not 0<=v<=255 for v in case['expected']):
            raise ValueError('fragment expected must be four bytes')
    else:
        for values in (case['expected'],case['inputs']['storage0']):
            if len(values)!=64 or any(type(v)!=int or not 0<=v<2**32 for v in values):
                raise ValueError('compute buffers must contain64 uint32 words')
    ops=[encode(s) for s in case['instructions']]
    if not ops or ops[-1]!=EXIT: raise ValueError('explicit EXIT required')
    header=0 if case['stage']=='compute' else 0x50
    n=(len(ops)+2)//3*3
    b=bytearray(header+n//3*32)
    if header:
        struct.pack_into('<I',b,0,2|(3<<5)|(5<<10));struct.pack_into('<I',b,72,15)
        for s in case['instructions']:
            if s.startswith('ALD '): b[24+int(s.split()[2])]=0xaa
    for i in range(n): struct.pack_into('<Q',b,header+(i//3*4+i%3+1)*8,ops[i] if i<len(ops) else EXIT)
    return b

def command(args,*,timeout=55,**kw):
    return subprocess.run([str(x) for x in args],check=True,timeout=timeout,**kw)

def tool(name):
    p=shutil.which(name)
    if not p and os.getenv('ANDROID_NDK_HOME'):
        q=Path(os.environ['ANDROID_NDK_HOME'])/'shader-tools/linux-x86_64'/name
        if q.is_file(): p=str(q)
    if not p: raise RuntimeError('missing '+name+'; install explicitly before tests (tests never download)')
    return p

def build(temp):
    src=temp/'shader-source'
    if not src.exists(): shutil.copytree(ROOT/'compat/tree/src/Ryujinx.Graphics.Shader',src,ignore=shutil.ignore_patterns('bin','obj'))
    opt=src/'Translation/Optimizations/Optimizer.cs'
    s=(ROOT/'compat/tree/src/Ryujinx.Graphics.Shader/Translation/Optimizations/Optimizer.cs').read_text()
    for name,call in [('ConstantFolding','ConstantFolding.RunPass(resourceManager, operation);'),('Simplification','Simplification.RunPass(operation);')]:
        assert s.count(call)==1
        s=s.replace(call,'string before'+name+'=global::IrEvidence.Snapshot(operation);\n                        '+call+'\n                        global::IrEvidence.Record("'+name+'",before'+name+',operation);')
    # Block-level pass: XmadOptimizer runs once per block before the per-operation folding loop (Optimizer.cs).
    call='XmadOptimizer.RunPass(context.Blocks[blkIndex]);'
    assert s.count(call)==1
    s=s.replace(call,'string beforeXmadOptimizer=global::IrEvidence.BlockSnapshot(context.Blocks[blkIndex]);\n                '+call+'\n                global::IrEvidence.RecordBlock("XmadOptimizer",beforeXmadOptimizer,context.Blocks[blkIndex]);')
    opt.write_text(s)
    context=src/'Translation/TranslatorContext.cs'
    s=(ROOT/'compat/tree/src/Ryujinx.Graphics.Shader/Translation/TranslatorContext.cs').read_text()
    call='            S2AReportStorageOffsetFindings(storageRange);'
    assert s.count(call)==1
    context.write_text(s.replace(call,'            global::IrEvidence.CaptureFinal(funcs);\n'+call))
    p=src/'Instructions/InstEmitFloatArithmetic.cs';s=p.read_text();start=s.index('public static void Fadd32i(');end=s.index('public static void FfmaR(',start)
    part=s[start:end];old='Operand srcB = GetSrcImm(context, op.Imm32);'
    if part.count(old)!=1: raise RuntimeError('FADD32I source injection point changed')
    injection=old+'''
            string mutation = System.Environment.GetEnvironmentVariable("S2A_TEST_FADD32I_MUTATION");
            if (mutation == "1") srcB = GetSrcImm(context, 0);
            else if (mutation != "0") throw new System.InvalidOperationException("test mutation must be explicitly 0 or 1");'''
    s=s[:start]+part.replace(old,injection)+s[end:];p.write_text(s)
    # In the same isolated build, remove only the .PO increment when requested.
    p=src/'Instructions/InstEmitIntegerArithmetic.cs'
    s=(ROOT/'compat/tree/src/Ryujinx.Graphics.Shader/Instructions/InstEmitIntegerArithmetic.cs').read_text()
    start=s.index('private static Operand PlusOne(')
    end=s.index('private static void SetPlusOneCarry(',start)
    part=s[start:end]
    old='            srcB = context.IAdd(srcB, Const(1));'
    if part.count(old)!=1: raise RuntimeError('IADD.PO source injection point changed')
    injection='''            string mutation = System.Environment.GetEnvironmentVariable("S2A_TEST_IADD_PO_MUTATION");
            if (mutation == "1") return null;
            if (mutation != "0") throw new System.InvalidOperationException("test mutation must be explicitly 0 or 1");
'''+old
    s = s[:start] + part.replace(old, injection) + s[end:]

    # Remove only the CF compensation, after the normal add and flag write.
    start = s.index('private static void SetPlusOneCarry(')
    end = s.index('private static void SetIaddFlags(', start)
    part = s[start:end]
    old = '            if (setCC && poCarry != null)'
    if part.count(old) != 1:
        raise RuntimeError('IADD.PO carry injection point changed')
    injection = f'''            string mutation = System.Environment.GetEnvironmentVariable("{IADD_PO_CARRY_MUTATION}");
            if (mutation == "1") return;
            if (mutation != "0") throw new System.InvalidOperationException("test mutation must be explicitly 0 or 1");
''' + old
    p.write_text(s[:start] + part.replace(old, injection) + s[end:])
    # 产物按「编译输入摘要」缓存在持久目录，命中不编（11-05 不稳定项 #12/#13：容器带 --rm、中间目录在临时区，
    #   原先每次在 amd64 模拟容器里全量冷编，10-31 撞 55 秒超时；4736ade9d 只把超时放宽到 900 秒，不算根修）。900 秒只当挂死兜底。
    key=build_key(src);cache=cache_root()
    hit=cache/key/'translator'/'ShaderCases.dll' if cache else None
    if hit and hit.is_file():
        os.utime(cache/key);(temp/'build.log').write_text(f'cache hit {key}\n');print(f'translator cache hit {key[:16]}',flush=True)
        return hit
    out=temp/'translator'
    command(['dotnet','build',HERE/'ShaderCases.csproj','-m:1','--configfile',HERE/'NuGet.Config',
             '-p:UseSharedCompilation=false',f'-p:ShaderRoot={src}',f'-p:BaseIntermediateOutputPath={temp}/obj/', '-o',out],timeout=900,stdout=open(temp/'build.log','w'))
    if cache: store_build(cache,key,out)
    return out/'ShaderCases.dll'

def cache_root():
    # 容器里由 tests/vulkan/run.sh 挂宿主持久目录到 /cache 并设 S2A_VULKAN_CACHE；Linux 原生跑用 XDG 缓存目录。S2A_VULKAN_CACHE=off 关缓存
    c=os.getenv('S2A_VULKAN_CACHE') or str(Path(os.getenv('XDG_CACHE_HOME') or Path.home()/'.cache')/'s2a_vulkan')
    if c=='off': return None
    root=Path(c)/'shader_cases'
    try: root.mkdir(parents=True,exist_ok=True)
    except OSError: return None
    return root

def build_key(src):
    # 摘要覆盖 dotnet build 的全部输入：注入后的着色器源（含 IrEvidence / 变异注入）、csproj 引入的 Spv.Generator 与 Ryujinx.Common、
    #   本目录 C# 与工程文件、NuGet 配置、global.json / Directory.Build.rsp、dotnet 版本、镜像 ID（run.sh 传入）、机器架构
    h=hashlib.sha256()
    def add(tag,base,files):
        for p in sorted(files):
            if 'obj' in p.relative_to(base).parts or 'bin' in p.relative_to(base).parts: continue
            h.update(tag.encode()+b'\0'+str(p.relative_to(base)).encode()+b'\0'+p.read_bytes()+b'\0')
    add('shader',src,[p for p in src.rglob('*') if p.is_file()])
    for d in ('compat/tree/src/Spv.Generator','compat/tree/src/Ryujinx.Common'):
        add(d,ROOT,(ROOT/d).rglob('*.cs'))
    add('here',ROOT,list(HERE.glob('*.cs'))+[HERE/'ShaderCases.csproj',HERE/'NuGet.Config'])
    add('root',ROOT,[p for p in (ROOT/'compat/tree/global.json',ROOT/'Directory.Build.rsp') if p.is_file()])
    h.update(b'dotnet\0'+subprocess.check_output(['dotnet','--version'],text=True).strip().encode())
    h.update(b'\0image\0'+os.getenv('S2A_VULKAN_IMAGE_ID','').encode()+b'\0arch\0'+os.uname().machine.encode())
    return h.hexdigest()

def store_build(cache,key,out,keep=8):
    # 先拷进同目录临时名再整目录 rename：并发两份门禁同键只有一份落地，另一份丢弃，读到的总是完整产物
    tmp=Path(tempfile.mkdtemp(prefix='.tmp-',dir=cache))
    try:
        shutil.copytree(out,tmp/'translator')
        try: os.rename(tmp,cache/key)
        except OSError: pass
    finally:
        if tmp.exists(): shutil.rmtree(tmp,ignore_errors=True)
    # 只留最近 keep 份（按目录 mtime），旧源码的产物自动清掉
    olds=sorted((p for p in cache.iterdir() if p.is_dir() and not p.name.startswith('.')),key=lambda p:p.stat().st_mtime,reverse=True)[keep:]
    for p in olds: shutil.rmtree(p,ignore_errors=True)

def source_head():
    # The container's /src may be a linked worktree whose .git points outside
    # the mount. The shared launcher reads HEAD on the host, never mounts Git
    # credentials, and this manifest still hashes every relevant source file.
    head=os.getenv('S2A_TEST_SOURCE_HEAD')
    origin='container-host-git' if head is not None else 'git'
    if head is None: head=subprocess.check_output(['git','-C',str(ROOT),'rev-parse','HEAD'],text=True).strip()
    if not re.fullmatch('[0-9a-f]{40}',head): raise ValueError('invalid source HEAD')
    return head,origin

def report_elapsed(count,start,build_elapsed):
    elapsed=time.monotonic()-start
    print(f'{count} cases, elapsed {elapsed:.3f}s; build {build_elapsed:.3f}s; excluding-build {elapsed-build_elapsed:.3f}s',flush=True)

def main():
    start=time.monotonic()
    dest=os.getenv('SHADER_CASES_OUT')
    holder=None
    if dest:
        temp=Path(dest).resolve();temp.mkdir(parents=True,exist_ok=False)
    else:
        holder=tempfile.TemporaryDirectory(prefix='shader-cases-');temp=Path(holder.name)
    cases=[json.loads(p.read_text()) for p in sorted(HERE.glob('*/*.json')) if p.parent.name not in ('obj','bin')]
    for family in ('integer','float','memory','attribute'):
        if sum(c['id'].startswith(family+'/') for c in cases)<2: raise ValueError('need at least2 cases per family: '+family)
    if {c['stage'] for c in cases}!={'compute','fragment'}: raise ValueError('both real stages required')
    ids=[c['id'] for c in cases]
    if len(ids)!=len(set(ids)) or not all(isinstance(i,str) and i for i in ids): raise ValueError('duplicate/invalid case IDs')
    po_index=ids.index(IADD_PO_CASE)
    po_case=cases[po_index]
    carry_index = ids.index(IADD_PO_CARRY_CASE)
    carry_case = cases[carry_index]
    manifest=[]
    for i,c in enumerate(cases):
        stem=temp/str(i);b=stem.with_suffix('.bin');b.write_bytes(binary(c))
        manifest.append({'id':c['id'],'stage':c['stage'],'binary':str(b),'spirv':str(stem.with_suffix('.spv'))})
    mf=temp/'manifest.json';mf.write_text(json.dumps(manifest))
    source_files=list((ROOT/'compat/tree/src/Ryujinx.Graphics.Shader').rglob('*.cs'))+list((ROOT/'compat/tree/src/Spv.Generator').rglob('*.cs'))+list(HERE.glob('*.cs'))+list(HERE.glob('*.py'))+list(HERE.glob('*/*.json'))
    source_files += [HERE/'vulkan_runner.c',HERE/'harness.vert',HERE/'ShaderCases.csproj',HERE/'NuGet.Config',ROOT/'tests/shader_cases_test.sh',ROOT/'android/native/direct/vk_device.c',ROOT/'android/native/direct/gs_path.c',ROOT/'compat/tree/global.json']
    head,head_origin=source_head()
    identity={'head':head,'head_source':head_origin,
              'sources':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(source_files) if 'obj' not in p.parts and 'bin' not in p.parts},
              'dotnet':subprocess.check_output(['dotnet','--version'],text=True).strip()}
    (temp/'identity.json').write_text(json.dumps(identity,indent=2))
    build_start=time.monotonic()
    dll=build(temp)
    build_elapsed=time.monotonic()-build_start
    identity['mutation']={'environment':'S2A_TEST_FADD32I_MUTATION','normal':'0','mutant':'1',
                          'dll_sha256':hashlib.sha256(dll.read_bytes()).hexdigest(),
                          'injected_source_sha256':hashlib.sha256((temp/'shader-source/Instructions/InstEmitFloatArithmetic.cs').read_bytes()).hexdigest(),
                          'normal_command':['dotnet',str(dll),str(mf),str(temp/'events.json')]+(['--probe-clamp'] if os.getenv('SHADER_CASES_PROBE_CLAMP')=='1' else []),
                          'mutant_command':['dotnet',str(dll),str(temp/'mutant-manifest.json'),str(temp/'mutant-events.json')]}
    po_entry=manifest[po_index]
    po_mutant=dict(po_entry,spirv=po_entry['spirv']+'.without-plus-one')
    po_mf=temp/'iadd-po-mutant-manifest.json'
    po_mf.write_text(json.dumps([po_mutant]))
    po_command=['dotnet',str(dll),str(po_mf),str(temp/'iadd-po-mutant-events.json')]
    po_source=temp/'shader-source/Instructions/InstEmitIntegerArithmetic.cs'
    identity['iadd_po_mutation']={'environment':'S2A_TEST_IADD_PO_MUTATION','normal':'0','mutant':'1',
                                 'injected_source_sha256':hashlib.sha256(po_source.read_bytes()).hexdigest(),
                                 'mutant_command':po_command}
    carry_entry = manifest[carry_index]
    carry_mutant = dict(carry_entry, spirv=carry_entry['spirv'] + '.without-plus-one-carry')
    carry_mf = temp / 'iadd-po-carry-mutant-manifest.json'
    carry_mf.write_text(json.dumps([carry_mutant]))
    carry_command = ['dotnet', str(dll), str(carry_mf), str(temp / 'iadd-po-carry-mutant-events.json')]
    identity['iadd_po_carry_mutation'] = {
        'environment': IADD_PO_CARRY_MUTATION, 'normal': '0', 'mutant': '1',
        'injected_source_sha256': identity['iadd_po_mutation']['injected_source_sha256'],
        'mutant_command': carry_command,
    }
    (temp/'identity.json').write_text(json.dumps(identity,indent=2))
    normal_env={**os.environ,'S2A_TEST_FADD32I_MUTATION':'0','S2A_TEST_IADD_PO_MUTATION':'0',
                IADD_PO_CARRY_MUTATION:'0'}
    command(identity['mutation']['normal_command'],env=normal_env)
    val=tool('spirv-val');glsl=tool('glslc')
    vs=temp/'harness.vert.spv'
    command([glsl,'--target-env=vulkan1.1',HERE/'harness.vert','-o',vs]);command([val,'--target-env','vulkan1.1',vs])
    includes=[]
    if os.getenv('VULKAN_HEADERS'): includes=['-I'+os.environ['VULKAN_HEADERS']]
    elif os.getenv('ANDROID_NDK_HOME'):
        hdr=Path(os.environ['ANDROID_NDK_HOME'])/'toolchains/llvm/prebuilt/linux-x86_64/sysroot/usr/include'
        # Isolate only Vulkan headers: never include an Android libc as host libc.
        for sub in ('vulkan','vk_video'):
            (temp/sub).symlink_to(hdr/sub,target_is_directory=True)
        includes=['-I'+str(temp)]
    runner=temp/'vulkan-runner'
    command([os.getenv('CC','cc'),'-std=c11','-O1',*includes,HERE/'vulkan_runner.c',ROOT/'android/native/direct/vk_device.c',ROOT/'android/native/direct/gs_path.c','-Wl,-l:libvulkan.so.1','-lm','-o',runner])
    def execute(c,m):
        check_device_capabilities(Path(m['spirv']).read_bytes())
        command([val,'--target-env','vulkan1.1',m['spirv']])
        out=temp/'readback.bin'
        if c['stage']=='compute':
            inp=temp/'input.bin'; inp.write_bytes(struct.pack('<64I',*c['inputs']['storage0']))
            command([runner,'compute',m['spirv'],inp,out]);return list(struct.unpack('<64I',out.read_bytes()))
        command([runner,'fragment',vs,m['spirv'],out]);return list(out.read_bytes())
    for c,m in zip(cases,manifest):
        got=execute(c,m)
        print(f"{c['id']}: expected {c['expected']} -> actual {got}",flush=True)
        compare(c['id'],c['expected'],got)
        if c is po_case:
            po_readback=got
        if c is carry_case:
            carry_readback = got
        if c['stage']=='compute' and os.getenv('SHADER_CASES_PROBE_CLAMP')=='1':
            cm=dict(m);cm['spirv']+='.clamped'
            clamped=execute(c,cm)
            compare(c['id']+' clamped',c['expected'],clamped)
            print(c['id']+': clamped readback matches all64 words',flush=True)
    # Effective negative controls use the same actual comparison, not a mocked result.
    bad=cases[0]['expected'].copy();bad[0]^=1
    expect_mismatch('wrong expectation '+cases[0]['id'],bad,execute(cases[0],manifest[0]))
    mm=[dict(m,spirv=m['spirv']+'.mutant') for m in manifest]
    mmf=temp/'mutant-manifest.json';mmf.write_text(json.dumps(mm))
    command(identity['mutation']['mutant_command'],env={**normal_env,'S2A_TEST_FADD32I_MUTATION':'1'})
    index=next(i for i,c in enumerate(cases) if c['id']=='float/add')
    positive_hash=hashlib.sha256(Path(manifest[index]['spirv']).read_bytes()).hexdigest()
    negative_hash=hashlib.sha256(Path(mm[index]['spirv']).read_bytes()).hexdigest()
    if positive_hash==negative_hash: raise AssertionError('mutation did not change emitted SPIR-V')
    (temp/'negative-proof.json').write_text(json.dumps({'positive_spirv':positive_hash,'negative_spirv':negative_hash,'expected':cases[index]['expected'],'mutation':identity['mutation']},indent=2))
    wrong=execute(cases[index],mm[index])
    expect_mismatch('wrong FADD32I emitter',cases[index]['expected'],wrong)
    command(po_command,env={**normal_env,'S2A_TEST_IADD_PO_MUTATION':'1'})
    po_without_plus_one=execute(po_case,po_mutant)
    report=collect_coverage(ROOT,manifest,json.loads((temp/'events.json').read_text()),
                            iadd_po_readbacks=(po_case['expected'],po_readback,po_without_plus_one))
    evidence=report['patches']['evidence'][IADD_PO_PATCH]
    evidence['spirv_sha256']={label:hashlib.sha256(Path(entry['spirv']).read_bytes()).hexdigest()
                             for label,entry in [('normal',po_entry),('without_plus_one',po_mutant)]}
    print(f"NEGATIVE rejected: {IADD_PO_CASE} without +1 changed {len(evidence['differences'])}/64 words",flush=True)
    command(carry_command, env={**normal_env, IADD_PO_CARRY_MUTATION: '1'})
    without_carry = execute(carry_case, carry_mutant)
    expected = carry_case['expected']
    # Word 9 is CF for A = 0, B = 0xffffffff. Keep every other word intact.
    carry_word = 9
    if expected[carry_word] != 1:
        raise AssertionError('IADD.PO carry case must expect 1 at word 9')
    mutant_expected = expected.copy()
    mutant_expected[carry_word] = 0
    expect_mismatch(IADD_PO_CARRY_CASE + ' without carry compensation', expected, without_carry)
    compare(IADD_PO_CARRY_CASE + ' control readback', mutant_expected, without_carry)
    carry_proof = {
        'case': IADD_PO_CARRY_CASE,
        'differences': [{'word': carry_word, 'normal': carry_readback[carry_word],
                         'without_plus_one_carry': without_carry[carry_word]}],
        'spirv_sha256': {label: hashlib.sha256(Path(entry['spirv']).read_bytes()).hexdigest()
                         for label, entry in [('normal', carry_entry), ('without_plus_one_carry', carry_mutant)]},
    }
    (temp / 'iadd-po-carry-proof.json').write_text(json.dumps(carry_proof, indent=2))
    print(f'NEGATIVE rejected: {IADD_PO_CARRY_CASE} word 9 changed 1 -> 0; other 63 words unchanged', flush=True)
    (temp/'coverage.json').write_text(json.dumps(report,indent=2))
    for key in ('instructions','passes','patches'):
        d=report[key]; print(f"{key}: {d['count']}/{d['total']}",flush=True)
    report_elapsed(len(cases),start,build_elapsed)
    if holder: holder.cleanup()
if __name__=='__main__': main()
