from app.services.auth_utils import is_admin_role, is_super_admin_role


def test_super_admin_is_admin_role():
    assert is_admin_role("admin")
    assert is_admin_role("super_admin")
    assert not is_admin_role("user")
    assert not is_admin_role("")


def test_only_super_admin_has_training_control_role():
    assert is_super_admin_role("super_admin")
    assert not is_super_admin_role("admin")
    assert not is_super_admin_role("user")
