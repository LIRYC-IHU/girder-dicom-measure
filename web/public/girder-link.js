/*
 * Intégration dans la vue item du client web Girder (injecté via l'index — cf. Dockerfile).
 * Sur une vue item DICOM, on ajoute :
 *   1) un panneau « Métadonnées DICOM » (façon plugin officiel dicom_viewer) ;
 *   2) un panneau « Mesures » (table des annotations : type, coupe, valeur, label, user, date) ;
 *   3) une ligne « Annoter » en bas de la section Info, vers /dmf/?itemId=<id>.
 * Données via les routes plugin /api/v1/dmf/item/:id/{dicom,annotations} (auth cookie).
 * Pur DOM, robuste aux ré-rendus (ré-application périodique + cache par item).
 */
(function () {
  var cache = {}; // itemId -> { dicom: meta|null, ann: [...] }
  var fetching = null;

  var TYPE_LABEL = {
    distance: 'Distance', point: 'Position', 'level-h': 'Niveau H', 'level-v': 'Niveau V',
  };

  // URL de base du viewer = celle de CE script (déduite de sa propre balise) → fonctionne
  // quel que soit le chemin de montage / préfixe de reverse-proxy, sans config.
  function viewerBase() {
    var tag = document.querySelector('script[src*="girder-link.js"]');
    return tag ? tag.src.replace(/girder-link\.js.*$/, '') : '/dmf/';
  }

  function itemIdFromHash() {
    var m = /#item\/([0-9a-fA-F]{12,})/.exec(window.location.hash);
    return m ? m[1] : null;
  }

  function esc(s) {
    var d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }

  function realInfo() {
    // La VRAIE section Info (on exclut nos propres panneaux qui réutilisent .g-item-info).
    return document.querySelector('.g-item-info:not(#dmf-dicom-panel):not(#dmf-measures-panel)');
  }

  function fmtValue(m) {
    var g = m.geometry || {}, v = m.values || {};
    if (m.type === 'distance') {
      return v.lengthMm != null ? v.lengthMm.toFixed(1) + ' mm' : Math.round(v.lengthPx || 0) + ' px';
    }
    if (m.type === 'point' && g.point) return '(' + Math.round(g.point.x) + ', ' + Math.round(g.point.y) + ')';
    if (m.type === 'level-h' && g.y != null) return 'y = ' + Math.round(g.y);
    if (m.type === 'level-v' && g.x != null) return 'x = ' + Math.round(g.x);
    return '';
  }

  function fmtDate(iso) {
    var d = new Date(iso);
    return isNaN(d.getTime()) ? (iso || '') : d.toLocaleString();
  }

  function makePanel(id, title, itemId) {
    var stale = document.getElementById(id);
    if (stale) stale.remove();
    var panel = document.createElement('div');
    panel.id = id;
    panel.className = 'g-item-info';
    panel.setAttribute('data-item-id', itemId);
    panel.style.cssText = 'margin-top:10px';
    var h = document.createElement('div');
    h.className = 'g-item-info-header';
    h.textContent = title;
    panel.appendChild(h);
    return panel;
  }

  function injectLink(info, itemId) {
    if (info.querySelector('#dmf-annotate-link')) return;
    var row = document.createElement('div');
    row.className = 'g-info-list-entry';
    row.id = 'dmf-annotate-link';
    row.style.cssText = 'margin-top:6px;font-weight:600';
    var a = document.createElement('a');
    a.href = viewerBase() + '?itemId=' + itemId;
    a.textContent = '🩻 Annoter (ouvrir le viewer DICOM)';
    row.appendChild(a);
    info.appendChild(row);
  }

  function injectDicom(info, itemId, meta) {
    var panel = makePanel('dmf-dicom-panel', 'Métadonnées DICOM', itemId);
    var body = document.createElement('div');
    body.style.cssText = 'max-height:320px;overflow:auto';
    body.innerHTML = Object.keys(meta).sort().map(function (k) {
      var v = meta[k];
      if (Array.isArray(v)) v = v.join(', ');
      return '<div class="g-info-list-entry" style="display:flex;justify-content:space-between;gap:1rem">' +
        '<span style="opacity:.65">' + esc(k) + '</span>' +
        '<span style="text-align:right;word-break:break-word">' + esc(String(v)) + '</span></div>';
    }).join('');
    panel.appendChild(body);
    info.parentNode.insertBefore(panel, info.nextSibling);
  }

  function injectMeasures(info, itemId, ann) {
    var panel = makePanel('dmf-measures-panel', 'Mesures (' + ann.length + ')', itemId);
    var rows = ann.map(function (m) {
      return '<tr style="border-top:1px solid #ddd">' +
        '<td style="padding:3px 6px">' + esc(TYPE_LABEL[m.type] || m.type) + '</td>' +
        '<td style="padding:3px 6px;text-align:center">' + (((m.frameIndex | 0) + 1)) + '</td>' +
        '<td style="padding:3px 6px">' + esc(fmtValue(m)) + '</td>' +
        '<td style="padding:3px 6px">' + esc(m.label || '') + '</td>' +
        '<td style="padding:3px 6px">' + esc((m.user && m.user.name) || '') + '</td>' +
        '<td style="padding:3px 6px;white-space:nowrap">' + esc(fmtDate(m.createdAt)) + '</td></tr>';
    }).join('');
    var body = document.createElement('div');
    body.style.cssText = 'max-height:320px;overflow:auto';
    body.innerHTML = '<table style="width:100%;border-collapse:collapse;font-size:13px">' +
      '<thead><tr style="opacity:.6;text-align:left">' +
      '<th style="padding:3px 6px">Type</th><th style="padding:3px 6px;text-align:center">Coupe</th>' +
      '<th style="padding:3px 6px">Valeur</th><th style="padding:3px 6px">Label</th>' +
      '<th style="padding:3px 6px">Utilisateur</th><th style="padding:3px 6px">Date</th>' +
      '</tr></thead><tbody>' + rows + '</tbody></table>';
    panel.appendChild(body);
    // Après le panneau DICOM s'il existe, sinon après la section Info.
    var anchor = document.getElementById('dmf-dicom-panel') || info;
    anchor.parentNode.insertBefore(panel, anchor.nextSibling);
  }

  function ensure() {
    var itemId = itemIdFromHash();
    if (!itemId) return;

    if (itemId in cache) {
      var c = cache[itemId];
      var hasMeasures = c.ann && c.ann.length;
      if (!c.dicom && !hasMeasures) return; // ni DICOM ni mesures → rien à ajouter
      var info = realInfo();
      if (!info) return; // vue item pas encore rendue
      if (c.dicom) {
        injectLink(info, itemId);
        if (!document.querySelector('#dmf-dicom-panel[data-item-id="' + itemId + '"]')) {
          injectDicom(info, itemId, c.dicom);
        }
      }
      if (hasMeasures && !document.querySelector('#dmf-measures-panel[data-item-id="' + itemId + '"]')) {
        injectMeasures(info, itemId, c.ann);
      }
      return;
    }

    if (fetching === itemId) return;
    fetching = itemId;
    var opt = { credentials: 'include' };
    Promise.all([
      fetch('/api/v1/dmf/item/' + itemId + '/dicom', opt).then(function (r) { return r.ok ? r.json() : null; }).catch(function () { return null; }),
      fetch('/api/v1/dmf/item/' + itemId + '/annotations', opt).then(function (r) { return r.ok ? r.json() : null; }).catch(function () { return null; }),
    ]).then(function (res) {
      fetching = null;
      var meta = res[0], ann = res[1];
      cache[itemId] = {
        dicom: meta && Object.keys(meta).length ? meta : null,
        ann: Array.isArray(ann) ? ann : [],
      };
    }).catch(function () { fetching = null; });
  }

  window.addEventListener('hashchange', ensure);
  window.addEventListener('popstate', ensure);
  setInterval(ensure, 700); // filet de sécurité contre les ré-rendus du client Girder
  ensure();
})();
