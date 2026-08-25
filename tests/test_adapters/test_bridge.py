"""Which bridge, if any, can answer for a practice."""


def test_a_working_link_outranks_a_skipped_wizard_answer():
    """The first real clinic could not find Eaglesoft in the PMS list, so they
    answered "skip". That answer alone kept bridge_name at None — meaning the
    agent would have stayed on our own book forever, with a perfectly good
    NexHealth calendar connected and every screen reporting success."""
    from types import SimpleNamespace

    from app.adapters.bridge import bridge_name

    skipped_but_connected = SimpleNamespace(
        pms_system="none",
        pms_credentials={"bridge": "nexhealth", "location_id": "1234",
                         "api_key": "k", "product_key": "pk"},
    )
    assert bridge_name(skipped_but_connected) == "nexhealth"


def test_no_credentials_and_no_system_is_still_no_bridge():
    """The guard still holds where it should: nothing set up, nothing claimed."""
    from types import SimpleNamespace

    from app.adapters.bridge import bridge_name

    assert bridge_name(SimpleNamespace(pms_system="none", pms_credentials=None)) is None
