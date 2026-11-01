from bridge.describeprobe import child_names, find_hits, size_report

DESC = {
    "name": "invoices",
    "attributes": [{"name": "InvoiceId", "type": "number"}, {"name": "InvoiceNumber", "type": "string"},
                   {"name": "HoldReason", "type": "string"}, {"name": "ApprovalStatus", "type": "string",
                                                              "description": "Shows whether the invoice is on hold"}],
    "children": [{"name": "invoiceLines"}, {"name": "invoiceHolds"}, {"name": "attachments"}],
    "actions": [{"name": "validateInvoice"}, {"name": "placeHold"}],
}


def test_size_is_reported_with_a_rough_token_count_and_the_limit_check():
    rep = size_report(DESC, cap_tokens=25000)
    assert rep["chars"] > 100 and rep["approx_tokens"] == rep["chars"] // 4 and rep["fits_in_one_result"] is True
    assert rep["top_level"]["attributes"]["items"] == 4 and rep["top_level"]["name"]["type"] == "str"
    big = {"attributes": [{"name": f"Field{i}", "description": "x" * 400} for i in range(400)]}
    assert size_report(big, cap_tokens=25000)["fits_in_one_result"] is False


def test_find_hits_locates_a_word_in_names_keys_and_descriptions():
    hits = find_hits(DESC, ["hold"])
    joined = "\n".join(hits)
    assert "attributes[HoldReason]" in joined                  # an attribute named HoldReason
    assert "children[invoiceHolds]" in joined                  # a child resource
    assert "actions[placeHold]" in joined                      # an action
    assert "on hold" in joined                                 # a description
    assert find_hits(DESC, ["nonexistentword"]) == []
    assert len(find_hits({"a": [{"name": f"hold{i}"} for i in range(100)]}, ["hold"], limit=5)) == 5


def test_search_is_case_insensitive_and_several_words_work():
    assert find_hits(DESC, ["HOLDREASON"])
    assert any("InvoiceNumber" in h for h in find_hits(DESC, ["hold", "invoicenumber"]))


def test_child_names_are_listed_from_a_children_section():
    assert child_names(DESC) == ["invoiceLines", "invoiceHolds", "attachments"]
    assert child_names({"children": ["a", "b"]}) == ["a", "b"]
    assert child_names({"nothing": 1}) == []


def test_each_matching_field_shows_its_type_and_flags_so_a_filter_can_be_written():
    d = {"attributes": [{"name": "ActiveHoldFlag", "type": "xsd:boolean", "queryable": True, "updatable": False,
                         "description": "Indicates an active hold"}]}
    hits = find_hits(d, ["hold"])
    line = next(h for h in hits if h.startswith("attributes[ActiveHoldFlag] ="))
    assert "type=xsd:boolean" in line and "queryable=True" in line and "name=" not in line
