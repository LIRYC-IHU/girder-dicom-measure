"""Renumérotation des coupes quand des fichiers SORTENT du stack (fonctions PURES).

Le viewer adresse une mesure par sa position dans le stack (`frameIndex`), stack construit à
partir de `item.dicom.files` : une entrée par fichier, développée en une position par frame
quand le serveur livre le fichier frame par frame (`GET /dmf/item/:id/files`, champ `frames`).
Retirer un fichier de cette liste — un objet DICOM sans pixels (PR, SR…) indexé par une
version antérieure à 0.6.0 — décale donc toutes les positions qui le suivent : sans
renumérotation, chaque mesure de l'item s'afficherait sur la coupe voisine.

Aucune dépendance Girder/Mongo → testable seul (cf. tests/test_stack.py).
"""


def removedPositions(layout, removed):
    """Positions (dans l'ANCIEN stack) occupées par les fichiers retirés.

    `layout` = `[(fileId, nombre de positions), …]` dans l'ancien ordre ; `removed` = ids des
    fichiers retirés. Renvoie `(positions triées, taille de l'ancien stack)`.
    """
    positions = []
    offset = 0
    for fileId, size in layout:
        size = max(int(size or 1), 1)
        if fileId in removed:
            positions.extend(range(offset, offset + size))
        offset += size
    return positions, offset


def remapIndex(index, positions, oldLength):
    """Nouvelle position d'une mesure : `(nouvel index, orpheline)`.

    Une position conservée recule du nombre de positions retirées AVANT elle. Une mesure posée
    SUR une position retirée est « orpheline » (elle ne désignait aucune image affichable) :
    elle est rattachée à la coupe qui la suit dans le nouveau stack, ou à la dernière s'il n'y
    en a pas ; `None` si le nouveau stack est vide. Rattachement par défaut, pas une preuve :
    le viewer < 0.6.0 laissait à l'écran la dernière image chargée avant l'échec — le plus
    souvent la coupe suivante (on arrive sur la coupe 0 en remontant depuis la 1) — et c'est
    sur ELLE que la géométrie a été tracée. L'appelant trace le rattachement (`orphan`).
    Un index non entier (absent, corrompu) est rendu tel quel.
    """
    if isinstance(index, bool) or not isinstance(index, int):
        return index, False
    removedSet = set(positions)
    before = sum(1 for p in positions if p < index)
    newLength = oldLength - len(removedSet)
    orphan = index in removedSet
    new = index - before
    if orphan:
        if newLength <= 0:
            return None, True
        new = min(new, newLength - 1)
    return new, orphan
