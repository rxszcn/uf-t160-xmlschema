#!/usr/bin/env python
#
# Copyright (c), 2016-2026, SISSA (International School for Advanced Studies).
# All rights reserved.
# This file is distributed under the terms of the MIT License.
# See the file 'LICENSE' in the root directory of the present
# distribution, or http://opensource.org/licenses/MIT.
#
"""
Tests for the loading-base (base URL) semantics of relative references.

Regression scenario: a schema text containing relative references is published
to several loading nodes. Loading it from an in-memory source used to resolve
the relative parts against the process working directory, so the result
depended on the node directory and was not reproducible. These tests pin the
two source forms to explicit, working-directory-independent base semantics.
"""
import os
import pathlib
import tempfile
import unittest
import warnings
from textwrap import dedent

from xmlschema import XMLSchema10, XMLSchema11, XMLSchemaBaseURLError, \
    XMLSchemaParseError
from xmlschema.resources.xml_resource import XMLResource
from xmlschema.testing import run_xmlschema_tests
from xmlschema.utils.urls import normalize_url, resolve_relative_url

MAIN_SCHEMA = dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
        <xs:include schemaLocation="included.xsd"/>
        <xs:element name="root" type="xs:string"/>
    </xs:schema>
""")

INCLUDED_SCHEMA = dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
        <xs:element name="included" type="xs:string"/>
    </xs:schema>
""")


@unittest.skipIf(
    not hasattr(os, 'chdir'),
    "the test needs to change the process working directory"
)
class TestLoadingBaseSemantics(unittest.TestCase):
    """Cross-working-directory, two-source-form comparison tests."""

    schema_class = XMLSchema10

    def setUp(self):
        self._cwd = os.getcwd()
        self.spec_dir = tempfile.mkdtemp(prefix='xmlschema_spec_')
        self.work_dir = tempfile.mkdtemp(prefix='xmlschema_cwd_')
        self.main_path = pathlib.Path(self.spec_dir).joinpath('main.xsd')
        self.included_path = pathlib.Path(self.spec_dir).joinpath('included.xsd')
        self.main_path.write_text(MAIN_SCHEMA, encoding='utf-8')
        self.included_path.write_text(INCLUDED_SCHEMA, encoding='utf-8')

    def tearDown(self):
        os.chdir(self._cwd)
        for path in (self.main_path, self.included_path):
            path.unlink(missing_ok=True)
        for directory in (self.spec_dir, self.work_dir):
            pathlib.Path(directory).rmdir()

    def test_resolve_relative_url_requires_explicit_base(self):
        # Absolute references and absolute local paths don't need a base.
        self.assertEqual(
            resolve_relative_url('https://example.com/a.xsd'),
            normalize_url('https://example.com/a.xsd')
        )
        abs_path = os.path.abspath('included.xsd')
        self.assertEqual(
            resolve_relative_url(abs_path),
            normalize_url(abs_path)
        )

        # Relative local references without a base fail explicitly and the
        # error names both the missing part and the base_url remedy.
        with self.assertRaises(XMLSchemaBaseURLError) as ctx:
            resolve_relative_url('included.xsd')
        message = str(ctx.exception)
        self.assertIn('included.xsd', message)
        self.assertIn('base_url', message)

        # keep_relative preserves the relative form instead of raising.
        self.assertEqual(
            resolve_relative_url('included.xsd', keep_relative=True),
            normalize_url('included.xsd', keep_relative=True)
        )

        # An explicit base resolves deterministically.
        base_url = pathlib.Path(self.spec_dir).as_uri() + '/'
        self.assertEqual(
            resolve_relative_url('included.xsd', base_url),
            normalize_url('included.xsd', base_url)
        )

    def test_in_memory_source_without_base_is_working_dir_independent(self):
        # Whatever the process working directory, an in-memory source without
        # a base URL must fail with the same explicit error (no cwd drift).
        errors = []
        for directory in (self.work_dir, self.spec_dir):
            os.chdir(directory)
            with self.assertRaises(XMLSchemaParseError) as ctx:
                self.schema_class(MAIN_SCHEMA)
            errors.append(str(ctx.exception))

        self.assertIn('included.xsd', errors[0])
        self.assertIn('base URL', errors[0])
        self.assertEqual(errors[0], errors[1])

    def test_file_source_base_semantics_are_preserved(self):
        # File-path sources keep resolving relative parts against the file
        # directory, independently from the process working directory.
        os.chdir(self.work_dir)
        with warnings.catch_warnings():
            warnings.simplefilter('error')
            schema = self.schema_class(str(self.main_path))

        included = list(schema.includes.values())[0]
        expected_url = normalize_url(str(self.included_path))
        self.assertEqual(included.url, expected_url)

        # Same result from a different working directory (reproducibility).
        os.chdir(self.spec_dir)
        schema_other = self.schema_class(str(self.main_path))
        included_other = list(schema_other.includes.values())[0]
        self.assertEqual(included_other.url, expected_url)

    def test_explicit_base_behavior_is_preserved(self):
        base_url = pathlib.Path(self.spec_dir).as_uri()
        os.chdir(self.work_dir)
        schema = self.schema_class(MAIN_SCHEMA, base_url=base_url)
        included = list(schema.includes.values())[0]
        self.assertEqual(included.url, normalize_url('included.xsd', base_url))

        # Passing the base as a plain directory path works too.
        schema_path_base = self.schema_class(MAIN_SCHEMA, base_url=self.spec_dir)
        self.assertTrue(schema_path_base.built)

    def test_two_forms_same_text_same_readings_across_working_dirs(self):
        """
        The key cross-form comparison: the SAME schema text is loaded once as
        a file-path source and once as an in-memory source with an explicit
        base. From two different working directories both forms must resolve
        the relative part to the identical URL and produce identical decoded
        readings.
        """
        base_url = pathlib.Path(self.spec_dir).as_uri()
        xml_instance = '<root>alpha</root>'
        readings = {}

        for working_dir in (self.work_dir, self.spec_dir):
            os.chdir(working_dir)

            file_schema = self.schema_class(str(self.main_path))
            memory_schema = self.schema_class(MAIN_SCHEMA, base_url=base_url)

            file_included = list(file_schema.includes.values())[0]
            memory_included = list(memory_schema.includes.values())[0]

            self.assertEqual(file_included.url, memory_included.url)
            self.assertEqual(
                file_included.url,
                normalize_url('included.xsd', base_url)
            )

            readings[working_dir] = (
                file_schema.decode(xml_instance),
                memory_schema.decode(xml_instance),
            )
            self.assertEqual(readings[working_dir][0], readings[working_dir][1])

        # Readings are identical across the two working directories for both forms.
        self.assertEqual(readings[self.work_dir], readings[self.spec_dir])

    def test_import_relative_part_still_satisfied_by_fallback_locations(self):
        # A relative import that can't be normalized without a base must not
        # short-circuit namespace loading: registered fallback locations still
        # satisfy the namespace (the established import resolution behavior).
        schema_text = dedent("""\
            <xs:schema targetNamespace="http://address0.com"
                       xmlns:xs="http://www.w3.org/2001/XMLSchema"
                       xmlns:ser="http://address0.com">
                <xs:import namespace="http://www.w3.org/2000/09/xmldsig#"
                    schemaLocation="xmldsig-core-schema.xsd"/>
                <xs:element name="class" type="ser:String32"/>
                <xs:simpleType name="String32">
                    <xs:restriction base="xs:normalizedString"/>
                </xs:simpleType>
            </xs:schema>
        """)
        os.chdir(self.work_dir)
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            schema = self.schema_class(schema_text)
        self.assertIn(
            'http://www.w3.org/2000/09/xmldsig#',
            schema.maps.namespaces
        )

    def test_xml_resource_distinguishes_source_kinds(self):
        # In-memory text source: no own URL, relative resolution needs a base.
        memory_resource = XMLResource(MAIN_SCHEMA)
        self.assertFalse(memory_resource.has_own_url)
        self.assertIsNone(memory_resource.url)
        with self.assertRaises(XMLSchemaBaseURLError):
            memory_resource.get_url('included.xsd')

        # File-path source: own URL provides the base, semantics preserved.
        file_resource = XMLResource(str(self.main_path))
        self.assertTrue(file_resource.has_own_url)
        expected_base = pathlib.Path(self.spec_dir).as_uri()
        self.assertEqual(file_resource.base_url, expected_base)
        # get_url keeps its established normalization path verbatim.
        self.assertEqual(
            file_resource.get_url('included.xsd'),
            normalize_url('included.xsd', file_resource._base_url)
        )

        # Passing an explicit base_url routes relative resolution to that base.
        based_file_resource = XMLResource(str(self.main_path), base_url=expected_base)
        self.assertEqual(
            based_file_resource.get_url('included.xsd'),
            normalize_url('included.xsd', expected_base)
        )


class TestLoadingBaseSemantics11(TestLoadingBaseSemantics):
    schema_class = XMLSchema11


if __name__ == '__main__':
    run_xmlschema_tests("loading base semantics")
