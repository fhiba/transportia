#!/usr/bin/env python
"""Django management entrypoint para el front del simulador Transportia."""
import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "webapp.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "No se pudo importar Django. Instalá las dependencias: pip install -r requirements.txt"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
