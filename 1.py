# =========================================================
# F1-02: FUNCTIONAL COLOUR PALETTE
# Detects actual rendered UI colours from visible elements.
# Ignores Bootstrap/framework CSS variable defaults.
# =========================================================

async def check_f1_02(page, url: str, colours: list) -> list:

    js_code = """
    return (function() {

        function normaliseHex(colour) {
            if (!colour) return null;
            var rgbMatch = colour.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)(?:,\\s*([\\d.]+))?\\)/);
            if (rgbMatch) {
                var a = rgbMatch[4] !== undefined ? parseFloat(rgbMatch[4]) : 1;
                if (a === 0) return null;
                var r = parseInt(rgbMatch[1]);
                var g = parseInt(rgbMatch[2]);
                var b = parseInt(rgbMatch[3]);
                return '#' + [r, g, b].map(function(v) {
                    return v.toString(16).padStart(2, '0');
                }).join('').toUpperCase();
            }
            if (colour.startsWith('#')) {
                var hex = colour.replace('#', '').toUpperCase();
                if (hex.length === 3) {
                    hex = hex.split('').map(function(c) { return c + c; }).join('');
                }
                return '#' + hex;
            }
            return null;
        }

        function isIgnoredColour(hex, property) {
            if (!hex) return true;
            var upper = hex.toUpperCase();
            if (upper === 'TRANSPARENT') return true;
            if (upper === '#FFFFFF' && property === 'backgroundColor') return true;
            if (upper === '#000000' && property === 'color') return true;
            var r = parseInt(upper.slice(1, 3), 16);
            var g = parseInt(upper.slice(3, 5), 16);
            var b = parseInt(upper.slice(5, 7), 16);
            if (property === 'backgroundColor' && r > 245 && g > 245 && b > 245) return true;
            if (property === 'color' && r < 30 && g < 30 && b < 30) return true;
            return false;
        }

        function isVisible(el) {
            try {
                var style = window.getComputedStyle(el);
                var rect  = el.getBoundingClientRect();
                return (
                    style.display    !== 'none'   &&
                    style.visibility !== 'hidden' &&
                    style.opacity    !== '0'      &&
                    rect.width        > 0         &&
                    rect.height       > 0
                );
            } catch(e) { return false; }
        }

        function getFrameworkRootColours() {
            var frameworkColours = [];
            try {
                var rootStyles = getComputedStyle(document.documentElement);
                var bsVars = [
                    '--bs-primary', '--bs-secondary', '--bs-success',
                    '--bs-info', '--bs-warning', '--bs-danger',
                    '--bs-light', '--bs-dark', '--bs-body-color',
                    '--bs-body-bg', '--bs-link-color', '--bs-link-hover-color',
                    '--bs-border-color', '--bs-heading-color'
                ];
                bsVars.forEach(function(varName) {
                    var val = rootStyles.getPropertyValue(varName).trim();
                    if (val) {
                        var hex = normaliseHex(val);
                        if (hex && frameworkColours.indexOf(hex) === -1) {
                            frameworkColours.push(hex);
                        }
                    }
                });
            } catch(e) {}
            return frameworkColours;
        }

        function classifySemanticColour(hex) {
            if (!hex) return null;
            var r = parseInt(hex.slice(1, 3), 16);
            var g = parseInt(hex.slice(3, 5), 16);
            var b = parseInt(hex.slice(5, 7), 16);
            var rN = r / 255, gN = g / 255, bN = b / 255;
            var max = Math.max(rN, gN, bN);
            var min = Math.min(rN, gN, bN);
            var l   = (max + min) / 2;
            var h = 0, s = 0;
            if (max !== min) {
                var d = max - min;
                s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
                if      (max === rN) h = ((gN - bN) / d + (gN < bN ? 6 : 0)) / 6;
                else if (max === gN) h = ((bN - rN) / d + 2) / 6;
                else                 h = ((rN - gN) / d + 4) / 6;
            }
            var hDeg = Math.round(h * 360);
            var sPct = Math.round(s * 100);
            var lPct = Math.round(l * 100);
            if (sPct < 25) return null;
            if (lPct < 10 || lPct > 90) return null;
            if (hDeg >= 85  && hDeg <= 165) return 'success';
            if (hDeg >= 35  && hDeg <= 84)  return 'warning';
            if (hDeg >= 0   && hDeg <= 20)  return 'error';
            if (hDeg >= 330 && hDeg <= 360) return 'error';
            if (hDeg >= 166 && hDeg <= 260) return 'info';
            return null;
        }

        function getSelector(el) {
            var sel = el.tagName.toLowerCase();
            if (el.id) sel += '#' + el.id;
            if (el.className && typeof el.className === 'string') {
                var classes = el.className.trim().split(/\\s+/).slice(0, 3);
                if (classes[0]) sel += '.' + classes.join('.');
            }
            return sel;
        }

        var UI_SELECTORS = [
            'button', '.btn',
            'input[type="button"]', 'input[type="submit"]', 'input[type="reset"]',
            '[role="button"]',
            'a', 'nav a', '.nav-link',
            'nav', '.navbar', '.nav', '.navigation',
            'header', '.header', '.site-header',
            '.menu', '.menu-item', '.dropdown-menu', '.dropdown-item',
            '.cta', '.call-to-action',
            '.tab', '.tabs', '[role="tab"]', '.nav-tabs .nav-link',
            '.badge', '.alert', '[role="alert"]',
            '.card', '.card-header', '.card-footer',
            '.pagination', '.page-item', '.page-link',
            'svg', 'svg path', 'svg circle', 'svg rect',
            '.icon', '[class*="icon"]',
            '[class*="btn"]', '[class*="button"]',
            '[class*="primary"]', '[class*="secondary"]'
        ];

        var PROPERTIES = [
            { css: 'backgroundColor', label: 'background-color' },
            { css: 'color',           label: 'color'            },
            { css: 'borderTopColor',  label: 'border-color'     },
            { css: 'outlineColor',    label: 'outline-color'    },
            { css: 'fill',            label: 'fill'             },
            { css: 'stroke',          label: 'stroke'           }
        ];

        var frameworkColours = getFrameworkRootColours();
        var colourData = {};
        var seen = [];
        var elements = [];

        function elInSeen(el) {
            for (var i = 0; i < seen.length; i++) {
                if (seen[i] === el) return true;
            }
            return false;
        }

        UI_SELECTORS.forEach(function(selector) {
            try {
                var found = document.querySelectorAll(selector);
                for (var i = 0; i < found.length; i++) {
                    var el = found[i];
                    if (!elInSeen(el)) {
                        seen.push(el);
                        elements.push({ el: el, selector: selector });
                    }
                }
            } catch(e) {}
        });

        function recordColour(hex, elSelector, property) {
            if (!hex) return;
            if (isIgnoredColour(hex, property)) return;
            if (!colourData[hex]) {
                colourData[hex] = { count: 0, selectors: [], properties: [] };
            }
            colourData[hex].count++;
            if (colourData[hex].selectors.indexOf(elSelector) === -1) {
                colourData[hex].selectors.push(elSelector);
            }
            if (colourData[hex].properties.indexOf(property) === -1) {
                colourData[hex].properties.push(property);
            }
        }

        elements.forEach(function(item) {
            var el = item.el;
            if (!isVisible(el)) return;
            try {
                var computed   = window.getComputedStyle(el);
                var elSelector = getSelector(el);

                PROPERTIES.forEach(function(prop) {
                    var value = computed.getPropertyValue(prop.css);
                    if (!value) return;
                    value = value.trim();
                    if (!value ||
                        value === 'inherit'      ||
                        value === 'currentColor' ||
                        value === 'initial'      ||
                        value === 'unset'        ||
                        value === 'transparent'  ||
                        value === 'none') return;
                    var hex = normaliseHex(value);
                    if (hex) recordColour(hex, elSelector, prop.label);
                });

                var tagName = el.tagName ? el.tagName.toLowerCase() : '';
                if (tagName === 'svg' || tagName === 'path' ||
                    tagName === 'circle' || tagName === 'rect' ||
                    el.closest('svg')) {
                    ['fill', 'stroke'].forEach(function(attr) {
                        var val = el.getAttribute(attr);
                        if (val && val !== 'none' && val !== 'currentColor') {
                            var hex = normaliseHex(val);
                            if (hex) recordColour(hex, elSelector, attr);
                        }
                    });
                }
            } catch(e) {}
        });

        var sorted = Object.keys(colourData).sort(function(a, b) {
            return colourData[b].count - colourData[a].count;
        });

        var interactiveColours = [];
        var semanticColours    = {};

        sorted.forEach(function(hex) {
            var data = colourData[hex];
            var sem  = classifySemanticColour(hex);
            if (sem) {
                if (!semanticColours[sem]) {
                    semanticColours[sem] = {
                        hex:        hex,
                        count:      data.count,
                        selectors:  data.selectors.slice(0, 5),
                        properties: data.properties
                    };
                }
            } else {
                interactiveColours.push({
                    hex:        hex,
                    count:      data.count,
                    selectors:  data.selectors.slice(0, 5),
                    properties: data.properties
                });
            }
        });

        return {
            interactiveColours:   interactiveColours,
            semanticColours:      semanticColours,
            frameworkColours:     frameworkColours,
            totalElementsScanned: elements.length
        };

    })();
    """

    try:
        data = await page.evaluate(js_code)
    except Exception as e:
        return [{
            "requirement": "F1-02",
            "name": "Functional Colour Palette",
            "status": "ERROR",
            "reason": f"F1-02 scan failed during browser execution: {str(e)}",
            "actual": "N/A",
            "expected": "Functional colours from rendered UI elements",
            "evidence": "",
        }]

    if not data:
        return [{
            "requirement": "F1-02",
            "name": "Functional Colour Palette",
            "status": "ERROR",
            "reason": "F1-02: No data returned from browser scan.",
            "actual": "N/A",
            "expected": "Functional colours from rendered UI elements",
            "evidence": "",
        }]

    interactive        = data.get("interactiveColours", [])
    semantic           = data.get("semanticColours", {})
    framework_excluded = data.get("frameworkColours", [])
    total_scanned      = data.get("totalElementsScanned", 0)

    primary   = interactive[0] if len(interactive) > 0 else None
    secondary = interactive[1] if len(interactive) > 1 else None

    evidence = await capture_page_screenshot(page, "F1_02", "functional_palette")

    if not primary and total_scanned == 0:
        return [{
            "requirement": "F1-02",
            "name": "Functional Colour Palette",
            "status": "WARN",
            "reason": (
                "No visible interactive UI elements found. "
                "Could not determine functional colour palette from rendered elements."
            ),
            "actual": "0 elements scanned",
            "expected": "Functional colours from rendered UI elements",
            "evidence": evidence,
        }]

    if not primary:
        return [{
            "requirement": "F1-02",
            "name": "Functional Colour Palette",
            "status": "WARN",
            "reason": (
                "Functional colour palette could not be determined. "
                "No significant interactive colours detected beyond white/black."
            ),
            "actual": f"{total_scanned} elements scanned, no qualifying colours found",
            "expected": "Functional colours from rendered UI elements",
            "evidence": evidence,
        }]

    reason_lines = [
        f"Primary functional colour {primary['hex']} detected "
        f"from {primary['count']} rendered UI elements.",
    ]

    if secondary:
        reason_lines.append(
            f"Secondary functional colour {secondary['hex']} detected "
            f"from {secondary['count']} rendered UI elements."
        )

    if semantic:
        reason_lines.append("Semantic colours detected:")
        for sem_type, sem_data in semantic.items():
            reason_lines.append(
                f"  {sem_type}: {sem_data['hex']} ({sem_data['count']} elements)"
            )

    if framework_excluded:
        reason_lines.append(
            f"Excluded {len(framework_excluded)} Bootstrap/framework default colour(s): "
            + ", ".join(framework_excluded)
        )

    return [{
        "requirement": "F1-02",
        "name": "Functional Colour Palette",
        "status": "PASS",
        "reason": "\n".join(reason_lines),
        "actual": (
            f"Primary: {primary['hex']} ({primary['count']} elements)"
            + (f" | Secondary: {secondary['hex']} ({secondary['count']} elements)" if secondary else "")
        ),
        "expected": "Functional colours detected from rendered UI elements (not framework defaults)",
        "evidence": evidence,
    }]