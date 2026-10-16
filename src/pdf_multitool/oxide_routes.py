"""Static module ownership for PDF Oxide's explicit operation contracts."""

import importlib

from .core import JobError

MODULE_NAMES = (
    "oxide_format",
    "oxide_creation",
    "oxide_editing",
    "oxide_forms",
    "oxide_conversion",
    "oxide_security",
    "oxide_analysis",
)


def modules():
    return [importlib.import_module("." + name, "pdf_multitool") for name in MODULE_NAMES]


def contracts():
    result = {}
    for module in modules():
        for name, contract in module.OPERATIONS.items():
            if name in result or not name.startswith("oxide_"):
                raise RuntimeError("PDF Oxide operation registry conflict")
            result[name] = contract
    return result


def owner(operation):
    for module in modules():
        if operation in module.OPERATIONS:
            return module
    raise JobError("Unsupported PDF Oxide operation")


def validate(operation, options):
    module = owner(operation)
    if hasattr(module, "validate_options"):
        module.validate_options(operation, options)


def process(request, options, add):
    from .oxide_runtime import require_runtime

    require_runtime(request)
    validate(request["operation"], options)
    return owner(request["operation"]).process(request, options, add)
