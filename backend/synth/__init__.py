def container_format(fmt: str) -> str:
    """On-disk numpy dtype for an engine sample_format. int12 is stored
    sign-extended in an int16 little-endian container."""
    return "int16" if fmt == "int12" else fmt
