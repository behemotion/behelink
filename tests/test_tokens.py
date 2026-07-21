from behelink import tokens


def test_generate_token_shape():
    tok = tokens.generate_token("blo")
    assert tok.startswith("blo_")
    assert len(tok) == 4 + 43
    assert tok[4:].isalnum()


def test_generate_token_unique():
    assert tokens.generate_token("blr") != tokens.generate_token("blr")


def test_hash_and_verify_roundtrip():
    tok = tokens.generate_token("blo")
    h = tokens.hash_token(tok)
    assert len(h) == 64
    assert tokens.verify_token(tok, h)
    assert not tokens.verify_token("blo_wrong", h)
