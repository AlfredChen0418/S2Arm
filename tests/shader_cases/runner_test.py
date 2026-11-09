#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 S2Arm contributors
import copy
import json
import os
import struct
from unittest.mock import patch
from pathlib import Path
import unittest
from run import binary, compare, encode, expect_mismatch, source_head, check_device_capabilities

class ContractTests(unittest.TestCase):
    def test_container_source_identity_is_explicit_and_bounded(self):
        with patch.dict(os.environ, {'S2A_TEST_SOURCE_HEAD':'a'*40}):
            self.assertEqual(('a'*40,'container-host-git'),source_head())
        for head in ('', 'a'*39, 'g'*40, 'a'*40+'\n'):
            with self.subTest(head=head), patch.dict(os.environ, {'S2A_TEST_SOURCE_HEAD':head}):
                with self.assertRaises(ValueError): source_head()

    def test_actual_spirv_device_capability_contract(self):
        header=struct.pack('<5I',0x07230203,0x10000,0,1,0)
        def instruction(op, *args): return struct.pack('<'+'I'*(len(args)+1),((len(args)+1)<<16)|op,*args)
        check_device_capabilities(header+instruction(17,1)) # Shader is allowed.
        for cap in (5363,5372,5378):
            with self.subTest(cap=cap), self.assertRaisesRegex(AssertionError,'not enabled'):
                check_device_capabilities(header+instruction(17,cap))
        name=b'SPV_EXT_fragment_shader_interlock\0'
        name+=b'\0'*(-len(name)%4)
        ext=struct.pack('<I',((1+len(name)//4)<<16)|10)+name
        with self.assertRaisesRegex(AssertionError,'not enabled'):
            check_device_capabilities(header+ext)
        for op in (5364,5365):
            with self.assertRaises(AssertionError): check_device_capabilities(header+instruction(op))
        for bad in (b'',header+b'x',header+struct.pack('<I',17),header+instruction(17)[:-1]):
            with self.assertRaises(ValueError): check_device_capabilities(bad)

    def fixture(self):
        return json.loads((Path(__file__).parent/'attribute/identity.json').read_text())
    def test_original_encoding(self):
        self.assertEqual(encode('EXIT'),0xe30000000007000f)
        self.assertEqual(encode('MOV32I R3 42'),0x0100000002a7f003)
        self.assertEqual(encode('LDG32 R2 R0 0'),0xeed4200000070002)
        self.assertEqual(encode('STG32 R2 R0 4'),0xeedc200000470002)
    def test_iadd_carry_encoding(self):
        # InstIaddR: .PO is AvgMode 3, .CC is bit 47, and .X is bit 43.
        self.assertEqual(encode('IADD.PO.CC R8 R2 R3'), 0x5c13800000370208)
        self.assertEqual(encode('IADD.X R9 RZ RZ'), 0x5c1008000ff7ff09)
        for line in ('IADD.PO.CC R8 R2 -1', 'IADD.X R9 RZ c[0][0]', 'ISCADD.PO.CC R8 R2 R3 0'):
            with self.subTest(line=line), self.assertRaises(ValueError):
                encode(line)

    def test_reject_unsupported_contract(self):
        for key,value in [('schema',2),('stage','geometry'),('expected',[0,0,0]),('inputs',{'texture0':[]}),('inputs',{'varying0':[1,2,3,4]})]:
            with self.subTest(key=key,value=value):
                c=self.fixture();c[key]=value
                with self.assertRaises(ValueError): binary(c)
    def test_uint32_buffer_validation(self):
        c=json.loads((Path(__file__).parent/'memory/copy17.json').read_text())
        for bad in ([1]*63,[2**32]*64,[-1]*64,[True]*64):
            with self.subTest(bad=bad[:1]):
                d=copy.deepcopy(c);d['inputs']['storage0']=bad
                with self.assertRaises(ValueError): binary(d)
    def test_same_comparator_for_real_negative(self):
        compare('normal',[1,2],[1,2])
        with self.assertRaises(AssertionError): compare('wrong',[1,2],[0,2])
        expect_mismatch('expected failure',[1,2],[0,2])
        with self.assertRaises(AssertionError): expect_mismatch('must fail if mutant survives',[1,2],[1,2])
    def test_no_unknown_instruction(self):
        with self.assertRaises(ValueError): encode('FAKE R0 7')
    def test_exit_and_header(self):
        c=self.fixture();b=binary(c)
        self.assertEqual(len(b[:0x50]),0x50)
        self.assertEqual(b[24],0xaa)
        c['instructions']=c['instructions'][:-1]
        with self.assertRaises(ValueError): binary(c)
if __name__=='__main__': unittest.main()
