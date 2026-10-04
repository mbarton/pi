from django.test import SimpleTestCase

from pi_durable.delta import PathError, UnsafePathError, apply


class ApplyTests(SimpleTestCase):
    def test_set_delete_and_replace(self):
        value = apply({"a": 1, "b": {"c": 2}}, [["s", ["b", "c"], 3], ["d", ["a"]], ["s", ["x"], [1]]])
        self.assertEqual(value, {"b": {"c": 3}, "x": [1]})
        self.assertEqual(apply({"a": 1}, [["r", {"z": 0}], ["s", ["y"], 1]]), {"z": 0, "y": 1})

    def test_array_operations(self):
        value = apply({"xs": [1, 2, 3]}, [["s", ["xs", 3], 4], ["d", ["xs", 0]], ["p", ["xs"], 1, 1, ["a", "b"]]])
        self.assertEqual(value, {"xs": [2, "a", "b", 4]})
        self.assertEqual(apply({"xs": ["a", "b", "c"]}, [["m", ["xs"], [2, 0, 1]]]), {"xs": ["c", "a", "b"]})
        self.assertEqual(apply([1, 2], [["p", [], 0, 2, []]]), [])

    def test_numeric_segments_address_object_keys_like_javascript(self):
        self.assertEqual(apply({"0": "a"}, [["a", ["0"], "b"], ["s", [1], "c"]]), {"0": "ab", "1": "c"})

    def test_trim_counts_utf16_code_units(self):
        # "😀" is two UTF-16 code units.
        self.assertEqual(apply({"t": "😀ab"}, [["t", ["t"], 2]]), {"t": "ab"})

    def test_rejects_unsafe_and_unresolvable_paths(self):
        with self.assertRaises(UnsafePathError):
            apply({}, [["s", ["__proto__", "x"], 1]])
        with self.assertRaises(UnsafePathError):
            apply({"xs": []}, [["s", ["xs", 5], 1]])
        with self.assertRaises(PathError):
            apply({"a": 1}, [["s", ["a", "b"], 1]])
        with self.assertRaises(PathError):
            apply({"a": 1}, [["a", ["a"], "x"]])
