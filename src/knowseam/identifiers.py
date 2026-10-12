"""Dependency-free identifier contract shared by configuration and capabilities."""
from typing import Annotated

from pydantic import Field

ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
Identifier = Annotated[str, Field(strict=True, pattern=ID_PATTERN, min_length=1, max_length=128)]
