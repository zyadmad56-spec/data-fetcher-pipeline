"""Local format conversion selected outside application policy."""


def convert_file(target_format: str, filepath: str, overwrite: bool = False) -> str:
    from scripts.format_alchemy import FormatAlchemyEngine
    output = FormatAlchemyEngine.convert(filepath, target_format, overwrite=overwrite)
    print(f'[FormatAlchemy] Converted to: {output}')
    return output
