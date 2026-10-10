def clean_text(value):
    return str(value or '').replace('\r\n', '\n').replace('\r', '\n').strip()
