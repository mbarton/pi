"""Python port of `apply()` from `@earendil-works/chord/delta`.

Storage materializes documents by replaying stored delta batches over the newest base. The op vocabulary is small:

    ["r", value]                         replace the root
    ["s", path, value]                   set a key or array index (index may append one past the end)
    ["d", path]                          delete a key, or splice out an array element
    ["a", path, text]                    append to a string
    ["t", path, count]                   drop the first `count` UTF-16 code units of a string
    ["p", path, index, remove, items]    array splice
    ["m", path, permutation]             reorder an array in place: new[i] = old[permutation[i]]

Keep this in sync with packages/chord/src/delta/index.ts. Values are plain parsed JSON (dict, list, str, int, float,
bool, None). Containers are mutated in place; pass a fresh `json.loads()` result.
"""

from __future__ import annotations

from typing import Any

RESERVED_SEGMENTS = frozenset({"__proto__", "constructor", "prototype"})


class DeltaError(ValueError):
    pass


class UnsafePathError(DeltaError):
    def __init__(self, segment: Any) -> None:
        super().__init__(f"unsafe path segment: {segment}")
        self.segment = segment


class PathError(DeltaError):
    def __init__(self, path: Any) -> None:
        super().__init__(f"unresolvable path: {path!r}")
        self.path = path


def _is_index(value: Any) -> bool:
    # JSON has one number type. JavaScript accepts 1.0 as an integer, Python parses it as a float.
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value >= 0
    return isinstance(value, float) and value.is_integer() and value >= 0


def _segment(value: Any) -> str | int:
    if isinstance(value, str):
        if value in RESERVED_SEGMENTS:
            raise UnsafePathError(value)
        return value
    if not _is_index(value):
        raise UnsafePathError(value)
    return int(value)


def _path(value: Any, non_empty: bool = False) -> list[str | int]:
    if not isinstance(value, list):
        raise DeltaError("path is not an array")
    if non_empty and not value:
        raise DeltaError("path is empty")
    return [_segment(segment) for segment in value]


def _permutation(value: Any) -> list[int]:
    if not isinstance(value, list):
        raise DeltaError("m permutation is not an array")
    seen = [False] * len(value)
    result: list[int] = []
    for index in value:
        if not _is_index(index) or int(index) >= len(value) or seen[int(index)]:
            raise DeltaError("m permutation is not a bijection")
        seen[int(index)] = True
        result.append(int(index))
    return result


def _validate(op: Any) -> None:
    if not isinstance(op, list) or not op:
        raise DeltaError("op is not a tuple")
    verb = op[0]
    arity = {"r": 2, "s": 3, "d": 2, "a": 3, "t": 3, "p": 5, "m": 3}.get(verb)
    if arity is None:
        raise DeltaError(f"unknown op verb: {verb}")
    if len(op) != arity:
        raise DeltaError(f"{verb} arity")
    if verb == "a" and not isinstance(op[2], str):
        raise DeltaError("a shape")
    if verb == "t" and not _is_index(op[2]):
        raise DeltaError("t shape")
    if verb == "p":
        if not _is_index(op[2]):
            raise DeltaError("p index")
        if not _is_index(op[3]):
            raise DeltaError("p remove")
        if not isinstance(op[4], list):
            raise DeltaError("p items")


def _key(container: Any, segment: str | int, path: list[str | int]) -> str | int:
    """Translate a segment the way JavaScript property access does."""
    if isinstance(container, list):
        if not isinstance(segment, int):
            raise UnsafePathError(segment)
        return segment
    if isinstance(container, dict):
        # JavaScript property keys are strings: obj[3] reads obj["3"].
        return str(segment)
    raise PathError(path)


def _resolve(root: Any, path: list[str | int]) -> Any:
    node = root
    for segment in path:
        key = _key(node, segment, path)
        if isinstance(node, list):
            if key >= len(node):  # type: ignore[operator]
                raise PathError(path)
        elif key not in node:
            raise PathError(path)
        node = node[key]
    if not isinstance(node, (dict, list)):
        raise PathError(path)
    return node


def _utf16_slice(value: str, start: int) -> str:
    """`value.slice(start)` with JavaScript's UTF-16 code unit indexing."""
    encoded = value.encode("utf-16-le", "surrogatepass")
    return encoded[start * 2 :].decode("utf-16-le", "surrogatepass")


def apply(target: Any, ops: list[Any]) -> Any:
    """Apply decoded ops to `target` in place and return the resulting root."""
    root = target
    for op in ops:
        _validate(op)
        verb = op[0]
        if verb == "r":
            root = op[1]
            continue

        path = _path(op[1], non_empty=verb in ("s", "d", "a", "t"))

        if verb == "p":
            array = root if not path else _resolve(root, path)
            if not isinstance(array, list):
                raise PathError(path)
            index, remove = int(op[2]), int(op[3])
            array[index : index + remove] = op[4]
            continue
        if verb == "m":
            array = root if not path else _resolve(root, path)
            permutation = _permutation(op[2])
            if not isinstance(array, list) or len(array) != len(permutation):
                raise PathError(path)
            previous = list(array)
            array[:] = [previous[index] for index in permutation]
            continue

        parent = _resolve(root, path[:-1])
        key = _key(parent, path[-1], path)
        is_array = isinstance(parent, list)
        if is_array and key > len(parent):  # type: ignore[operator]
            raise UnsafePathError(key)
        present = key < len(parent) if is_array else key in parent  # type: ignore[operator]

        if verb == "d":
            if is_array and not present:
                raise PathError(path)
            if present:
                del parent[key]
            continue
        if verb == "s":
            value = op[2]
        else:
            current = parent[key] if present else None
            if not isinstance(current, str):
                raise PathError(path)
            value = current + op[2] if verb == "a" else _utf16_slice(current, int(op[2]))
        if is_array and key == len(parent):
            parent.append(value)
        else:
            parent[key] = value
    return root
