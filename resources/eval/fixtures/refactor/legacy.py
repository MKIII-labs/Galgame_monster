def process_data(rows):
    out = []
    for r in rows:
        v = str(r or "").strip()
        if v:
            out.append(v.lower())
    return out


def total(rows):
    return len(process_data(rows))
