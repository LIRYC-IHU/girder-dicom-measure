/*
 * Intégration dans la vue item du client web Girder (chargé par Girder lui-même, le plugin
 * le déclarant via `registerPluginStaticContent`).
 * Sur une vue item DICOM, on ajoute :
 *   1) un panneau « Métadonnées DICOM » (façon plugin officiel dicom_viewer) ;
 *   2) un panneau « Mesures » (table des annotations : type, coupe, valeur, label, user, date) ;
 *   3) une ligne « Annoter » en bas de la section Info, vers /dmf/?itemId=<id>.
 * Données via les routes plugin /api/v1/dmf/item/:id/{dicom,annotations} (auth cookie).
 * Pur DOM, robuste aux ré-rendus (ré-application périodique + cache par item).
 *
 * Et — c'est l'autre moitié du fichier — la PAGE DE CONFIGURATION du plugin dans la console
 * d'administration de Girder (roue dentée de la liste des plugins → « Configurer »). Girder 5
 * charge les scripts de plugin AVANT d'initialiser son application et publie tout son cœur sur
 * `window.girder` (Backbone, vues, router, rest…) : on enregistre donc une vraie vue et une
 * vraie route Girder, comme le ferait un plugin natif, SANS reconstruire son client.
 */
(function () {
  // Le script peut être chargé deux fois (déclaration `registerPluginStaticContent` ET
  // balise <script> injectée par un ancien déploiement) : une seule instance doit vivre,
  // sinon on enregistrerait la route d'admin en double.
  if (window.__dmfGirderLinkLoaded) { return; }
  window.__dmfGirderLinkLoaded = true;

  var cache = {}; // itemId -> { dicom: meta|null, ann: [...] }
  var fetching = null;
  var viewerPath = null; // chemin du viewer, lu sur /api/v1/dmf/config

  var TYPE_LABEL = {
    distance: 'Distance', point: 'Position', 'level-h': 'Niveau H', 'level-v': 'Niveau V',
  };

  // Le script est servi depuis /plugin_static/dicom_measure_flow/, pas depuis le viewer :
  // son URL ne dit rien du chemin de montage de la SPA. On lit donc `dmf.viewer_path` sur
  // la route publique du plugin. Tant que la réponse n'est pas là, on n'injecte rien
  // (plutôt qu'un lien qu'il faudrait corriger après coup).
  function viewerBase() {
    return viewerPath + '/';
  }

  function loadConfig() {
    return fetch('/api/v1/dmf/config', { credentials: 'include' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (cfg) {
        viewerPath = (cfg && cfg.viewerPath) || '/dmf';
      })
      .catch(function () { viewerPath = '/dmf'; });
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

  // --- Page de configuration du plugin, DANS la console d'admin de Girder ---
  //
  // `exposePluginConfig` fait apparaître la roue dentée sur la ligne du plugin
  // (page « Plugins »), et la route rend notre vue dans le corps de l'application. Les
  // valeurs viennent de `GET /api/v1/dmf/settings` (défauts + modes valides inclus), la
  // validation reste côté serveur (validateurs `setting_utilities`).
  var PLUGIN = 'dicom_measure_flow';
  var CONFIG_ROUTE = 'plugins/' + PLUGIN + '/config';
  var adminRegistered = false;

  // Description des champs : un seul rendu générique, l'ordre ici = l'ordre à l'écran.
  var FIELDS = [
    {
      key: 'dmf.compression', type: 'select', label: 'Compression des pixels',
      help: 'Les DICOM sont souvent stockés non compressés : une boucle de scopie pèse alors ' +
            'plusieurs dizaines de méga-octets. Le serveur recompresse à la volée (et met le ' +
            "résultat en cache) avant l'envoi au navigateur. Les fichiers déjà compressés sont " +
            'servis tels quels.',
      options: {
        none: 'Aucune — fichier original',
        lossless: 'Sans perte (JPEG-LS) — pixels identiques',
        lossy: 'Avec perte (JPEG 2000) — ratio réglable',
      },
    },
    {
      key: 'dmf.lossy_ratio', type: 'number', label: 'Ratio visé (avec perte)', unit: ': 1',
      lossyOnly: true, min: 2, max: 50, step: 1,
      help: 'Entre 2 et 50. À 10:1 le rendu reste visuellement proche de l\'original, mais les ' +
            'pixels sont modifiés : valider sur vos propres images avant de passer en « avec perte ».',
    },
    {
      key: 'dmf.compression_max_mb', type: 'number', label: 'Taille maximale transcodée',
      unit: 'Mo', min: 0, step: 64,
      help: "Au-delà, le fichier est envoyé tel quel (l'encodage charge les pixels en mémoire). " +
            '0 = pas de limite.',
    },
    {
      key: 'dmf.cache_dir', type: 'text', label: 'Dossier du cache',
      placeholder: '(dossier temporaire du système)',
      help: 'Chemin absolu. Vide = dossier temporaire du système, effacé au redémarrage de la ' +
            'machine ou du conteneur.',
    },
    {
      key: 'dmf.cache_max_mb', type: 'number', label: 'Taille maximale du cache', unit: 'Mo',
      min: 0, step: 256,
      help: 'Éviction des entrées les moins récemment lues. 0 = pas de limite.',
    },
    {
      key: 'dmf.viewer_path', type: 'text', label: 'Chemin du viewer',
      help: 'Un seul segment, commençant par « / ». Prend effet au REDÉMARRAGE de Girder ' +
            '(les autres réglages sont appliqués immédiatement).',
    },
  ];

  function fieldId(key) {
    return 'dmf-' + key.replace(/[^a-z0-9]+/g, '-');
  }

  function fieldHtml(f) {
    var id = fieldId(f.key), input;
    if (f.type === 'select') {
      input = '<select class="form-control" id="' + id + '">' +
        Object.keys(f.options).map(function (v) {
          return '<option value="' + esc(v) + '">' + esc(f.options[v]) + '</option>';
        }).join('') + '</select>';
    } else {
      input = '<input class="form-control" id="' + id + '" type="' + f.type + '"' +
        (f.min !== undefined ? ' min="' + f.min + '"' : '') +
        (f.max !== undefined ? ' max="' + f.max + '"' : '') +
        (f.step !== undefined ? ' step="' + f.step + '"' : '') +
        (f.placeholder ? ' placeholder="' + esc(f.placeholder) + '"' : '') + '>';
    }
    return '<div class="form-group" data-dmf-field="' + esc(f.key) + '">' +
      '<label class="control-label" for="' + id + '">' + esc(f.label) + '</label>' +
      '<div style="display:flex;align-items:center;gap:.5rem;max-width:34rem">' + input +
      (f.unit ? '<span style="white-space:nowrap;opacity:.7">' + esc(f.unit) + '</span>' : '') +
      '</div>' +
      '<p class="help-block">' + esc(f.help) + '</p></div>';
  }

  function registerAdminConfig() {
    var g = window.girder;
    if (adminRegistered || !g || !g.views || !g.utilities || !g.router) return;
    adminRegistered = true;

    g.utilities.PluginUtils.exposePluginConfig(PLUGIN, CONFIG_ROUTE);

    var MODE_ID = fieldId('dmf.compression');

    var ConfigView = g.views.View.extend({
      // Clé calculée : l'id du <select> est dérivé de la clé de réglage (cf. `fieldId`),
      // l'écrire en dur ici le désynchroniserait du template.
      events: {
        'click #dmf-save': function () { this._save(); },
        'click #dmf-defaults': function () {
          this._fill(this.settings.defaults);
        },
        ['change #' + MODE_ID]: function () { this._syncMode(); },
      },

      initialize: function () {
        g.rest.restRequest({ url: 'dmf/settings', method: 'GET' }).done((resp) => {
          this.settings = resp;
          this.render();
        });
      },

      render: function () {
        this.$el.html(
          '<div class="g-body-title">DICOM Measure Flow</div>' +
          '<p class="help-block" style="margin-bottom:1.5rem">Transport des pixels DICOM vers ' +
          'le viewer d\'annotation, et emplacement de celui-ci.</p>' +
          '<form id="dmf-config-form" onsubmit="return false">' +
          FIELDS.map(fieldHtml).join('') +
          '<button class="btn btn-sm btn-primary" id="dmf-save">Enregistrer</button> ' +
          '<button class="btn btn-sm btn-default" id="dmf-defaults">Valeurs par défaut</button>' +
          '<div id="dmf-error" class="g-validation-failed-message"></div>' +
          '</form>'
        );
        this._fill(this.settings.values);
        return this;
      },

      _fill: function (values) {
        FIELDS.forEach((f) => {
          this.$('#' + fieldId(f.key)).val(values[f.key] === undefined ? '' : values[f.key]);
        });
        this._syncMode();
      },

      _syncMode: function () {
        var lossy = this.$('#' + MODE_ID).val() === 'lossy';
        FIELDS.filter((f) => f.lossyOnly).forEach((f) => {
          this.$('[data-dmf-field="' + f.key + '"]')
            .css('opacity', lossy ? '' : 0.5)
            .find('input').prop('disabled', !lossy);
        });
      },

      _save: function () {
        var payload = {};
        FIELDS.forEach((f) => {
          var raw = this.$('#' + fieldId(f.key)).val();
          payload[f.key] = f.type === 'number' ? Number(raw) : raw;
        });
        this.$('#dmf-error').text('');
        g.rest.restRequest({
          url: 'dmf/settings',
          method: 'PUT',
          data: JSON.stringify(payload),
          contentType: 'application/json',
          error: null, // erreurs affichées dans le formulaire, pas en bandeau global
        }).done((saved) => {
          this.settings.values = saved;
          this._fill(saved);
          g.events.trigger('g:alert', {
            text: 'Réglages enregistrés.', type: 'success', icon: 'ok', timeout: 3000,
          });
        }).fail((resp) => {
          this.$('#dmf-error').text((resp.responseJSON && resp.responseJSON.message) ||
            'Enregistrement impossible.');
        });
      },
    });

    g.router.route(CONFIG_ROUTE, 'dmfConfig', function () {
      g.events.trigger('g:navigateTo', ConfigView);
    });
  }

  function ensure() {
    registerAdminConfig();
    if (viewerPath === null) return; // config pas encore lue

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
  loadConfig().then(ensure);
})();
