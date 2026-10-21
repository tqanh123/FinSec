"""ProjectDescriptor model, auto-discovery, and the one-door store."""
from .autodiscover import discover_roots
from .model import (COPY_KINDS, CopiesSpec, DescriptorError, DriverSpec, EntrypointsSpec,
                    GeneratedFile,
                    ProjectDescriptor, ROOT_KINDS, Root, DESCRIPTOR_SCHEMA_VERSION,
                    ScanSpec, as_driver_spec, as_entrypoints_spec, as_scan_spec,
                    check_overlap, check_roots, duplicate_keys, require_roots,
                    root_paths, roots_banner, scan_py, scan_py_counted, select_roots)
from .store import (LegacyDescriptorCodec, LoadedDescriptor, check_python,
                    check_repo, load, register_legacy_codec)

__all__ = [
    "COPY_KINDS", "CopiesSpec",
    "DescriptorError", "DriverSpec", "EntrypointsSpec", "GeneratedFile",
    "ProjectDescriptor", "ROOT_KINDS", "Root", "as_driver_spec", "as_entrypoints_spec",
    "DESCRIPTOR_SCHEMA_VERSION", "ScanSpec", "as_scan_spec", "check_overlap",
    "check_repo", "check_roots", "require_roots", "root_paths", "roots_banner",
    "scan_py", "scan_py_counted",
    "select_roots", "discover_roots", "LoadedDescriptor",
    "check_python", "duplicate_keys", "load",
    "LegacyDescriptorCodec", "register_legacy_codec",
]
