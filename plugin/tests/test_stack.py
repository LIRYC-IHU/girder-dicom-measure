"""Renumérotation des coupes quand des objets non-image sortent du stack (pur)."""

import stack  # chargé via conftest (dossier du package sur sys.path)


def test_presentation_state_before_a_cine_loop():
    """Cas DEFINE-PFA (Rhéna) : [PR (1 position), boucle XA (69 frames)]."""
    positions, oldLength = stack.removedPositions([("pr", 1), ("xa", 69)], {"pr"})
    assert (positions, oldLength) == ([0], 70)
    assert stack.remapIndex(1, positions, oldLength) == (0, False)
    assert stack.remapIndex(69, positions, oldLength) == (68, False)
    # Posée SUR le PR : rattachée à la coupe qui suit, signalée orpheline.
    assert stack.remapIndex(0, positions, oldLength) == (0, True)


def test_non_image_in_the_middle_only_shifts_what_follows():
    positions, oldLength = stack.removedPositions(
        [("a", 3), ("sr", 1), ("b", 2), ("ko", 1)], {"sr", "ko"}
    )
    assert (positions, oldLength) == ([3, 6], 7)
    assert [stack.remapIndex(i, positions, oldLength)[0] for i in (0, 2, 4, 5)] == [0, 2, 3, 4]
    assert stack.remapIndex(3, positions, oldLength) == (3, True)
    # Orpheline en fin de stack : rattachée à la dernière coupe restante.
    assert stack.remapIndex(6, positions, oldLength) == (4, True)


def test_nothing_left_to_attach_to():
    positions, oldLength = stack.removedPositions([("pr", 1)], {"pr"})
    assert stack.remapIndex(0, positions, oldLength) == (None, True)


def test_non_integer_index_is_left_alone():
    positions, oldLength = stack.removedPositions([("pr", 1), ("xa", 3)], {"pr"})
    assert stack.remapIndex(None, positions, oldLength) == (None, False)
    assert stack.remapIndex(True, positions, oldLength) == (True, False)


def test_sizes_below_one_count_as_one_position():
    positions, oldLength = stack.removedPositions([("pr", 0), ("xa", None)], {"pr"})
    assert (positions, oldLength) == ([0], 2)
