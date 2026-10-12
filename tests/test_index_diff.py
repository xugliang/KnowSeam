def test_insert_at_start_and_duplicate_payloads_keep_distinct_bindings():
    from knowseam.indexing.diff import plan_diff
    old = ['a', 'b', 'a']
    result = plan_diff(old, ['new', 'a', 'b', 'a'])
    assert result.reused == ((0, 1), (1, 2), (2, 3))
    assert result.added == (0,) and result.removed == ()
    assert plan_diff(['a', 'a'], ['a']).removed == (1,)


def test_token_payload_identity_separates_company_dictionary_and_input():
    from knowseam.indexing.cache import payload_key
    base = payload_key('c1', 'fp1', 'text')
    assert len({base, payload_key('c2', 'fp1', 'text'), payload_key('c1', 'fp2', 'text'),
                payload_key('c1', 'fp1', 'new')}) == 4
