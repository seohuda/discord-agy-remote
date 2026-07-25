from dishell.terminal import effective_size


def test_without_remote_control_the_terminal_size_is_used():
    assert effective_size((100, 30), remote_cols=60, remote_active=False) == (100, 30)


def test_remote_control_narrows_the_screen_for_discord():
    assert effective_size((100, 30), remote_cols=60, remote_active=True) == (60, 30)


def test_a_terminal_narrower_than_the_target_is_left_alone():
    """Widening past the real terminal would wrap badly on the local screen."""
    assert effective_size((45, 30), remote_cols=60, remote_active=True) == (45, 30)


def test_zero_disables_narrowing():
    assert effective_size((100, 30), remote_cols=0, remote_active=True) == (100, 30)


def test_rows_are_never_changed():
    assert effective_size((100, 24), remote_cols=60, remote_active=True)[1] == 24


def test_a_pathologically_small_width_is_floored():
    assert effective_size((10, 30), remote_cols=60, remote_active=True) == (20, 30)
