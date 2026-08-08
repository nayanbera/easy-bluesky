"""peak_fit.py — Curve-fitting models (peaks + steps) using lmfit."""

import numpy as np

try:
    import lmfit
    LMFIT_AVAILABLE = True
except ImportError:
    LMFIT_AVAILABLE = False

# Keep for legacy guard checks
try:
    from scipy.optimize import curve_fit as _curve_fit  # noqa: F401
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False

# ── Model catalog ──────────────────────────────────────────────────────────────

PEAK_MODELS       = ["Gaussian", "Lorentzian", "Voigt", "Pseudo-Voigt", "Super-Gaussian"]
STEP_MODELS       = ["Step (erf)", "Step (tanh)", "Step (arctan)", "Step (logistic)"]
MODELS            = PEAK_MODELS + STEP_MODELS
BACKGROUND_MODELS = ["None", "Constant", "Linear", "Quadratic", "Cubic"]

MINIMIZERS = [
    ("Levenberg-Marquardt",    "leastsq"),
    ("Least Squares",          "least_squares"),
    ("Nelder-Mead",            "nelder"),
    ("L-BFGS-B",               "lbfgsb"),
    ("Powell",                 "powell"),
    ("Differential Evolution", "differential_evolution"),
]
MINIMIZER_NAMES = [m[0] for m in MINIMIZERS]
MINIMIZER_KEYS  = {m[0]: m[1] for m in MINIMIZERS}

# ── Parameter display labels ───────────────────────────────────────────────────

_PARAM_LABEL = {
    "amplitude": "Amplitude",
    "center":    "Center (x₀)",
    "sigma":     "Sigma (σ)",
    "gamma":     "Gamma (γ)",
    "fraction":  "Lorentz fraction",
    "exponent":  "Exponent (n)",
    "bg_c0":     "Background c₀",
    "bg_c1":     "Background c₁",
    "bg_c2":     "Background c₂",
    "bg_c3":     "Background c₃",
}

# ── Model functions ────────────────────────────────────────────────────────────

def _gaussian_fn(x, amplitude, center, sigma):
    return amplitude * np.exp(-0.5 * ((x - center) / sigma) ** 2)

def _lorentzian_fn(x, amplitude, center, sigma):
    return amplitude / (1.0 + ((x - center) / sigma) ** 2)

def _supergaussian_fn(x, amplitude, center, sigma, exponent):
    return amplitude * np.exp(-(((x - center) ** 2) / (2 * sigma ** 2)) ** exponent)

def _erf_step_fn(x, amplitude, center, sigma):
    from scipy.special import erfc
    # sigma may be negative to produce a decreasing step
    return amplitude * erfc(-(x - center) / sigma) / 2.0

def _arctan_step_fn(x, amplitude, center, sigma):
    # sigma may be negative to produce a decreasing step
    return amplitude * (0.5 + np.arctan((x - center) / sigma) / np.pi)

def _logistic_step_fn(x, amplitude, center, sigma):
    # sigma may be negative to produce a decreasing step
    with np.errstate(over="ignore"):
        return amplitude / (1.0 + np.exp(-(x - center) / sigma))

def _tanh_step_fn(x, amplitude, center, sigma):
    # sigma may be negative to produce a decreasing step
    return amplitude * (np.tanh((x - center) / sigma) + 1.0) / 2.0

# ── Initial-guess helpers ──────────────────────────────────────────────────────

def _guess_peak(x, y):
    i_max     = int(np.argmax(np.abs(y)))
    amplitude = float(y[i_max])
    center    = float(x[i_max])
    above     = x[y * np.sign(amplitude) >= abs(amplitude) * 0.5]
    if len(above) >= 2:
        hw = (float(above[-1]) - float(above[0])) / 2.0
    else:
        hw = (float(x[-1]) - float(x[0])) / 4.0
    step  = abs(float(x[1] - x[0])) if len(x) > 1 else 1.0
    hw    = max(hw, step)
    sigma = hw / np.sqrt(2.0 * np.log(2.0))
    return amplitude, center, sigma

def _guess_step(x, y):
    dy        = np.gradient(y, x)
    i_c       = int(np.argmax(np.abs(dy)))
    center    = float(x[i_c])
    amplitude = float(y[-1] - y[0])
    span      = float(x[-1] - x[0])
    step      = abs(float(x[1] - x[0])) if len(x) > 1 else 1.0
    mag       = max(span / 8.0, step)
    # Negative sigma flips the step direction — use it for decreasing steps
    # so the optimizer starts with the correct shape rather than an inverted one.
    sigma = mag if amplitude >= 0 else -mag
    return amplitude, center, sigma

# ── Background model factory ───────────────────────────────────────────────────

_BG_DEGREE = {"Constant": 0, "Linear": 1, "Quadratic": 2, "Cubic": 3}

def make_background_model(bg_name: str):
    """Return a PolynomialModel with prefix 'bg_', or None for 'None'."""
    if bg_name == "None":
        return None
    if not LMFIT_AVAILABLE:
        raise RuntimeError("lmfit not installed — pip install lmfit")
    degree = _BG_DEGREE.get(bg_name)
    if degree is None:
        raise ValueError(f"Unknown background model: {bg_name!r}")
    return lmfit.models.PolynomialModel(degree=degree, prefix="bg_")

def _guess_background(x, y, bg_name: str) -> dict:
    """Estimate background polynomial coefficients from the data edges."""
    if bg_name == "None":
        return {}
    degree = _BG_DEGREE[bg_name]
    n = len(x)
    n_edge = max(3, n // 5)
    x_edge = np.concatenate([x[:n_edge], x[-n_edge:]])
    y_edge = np.concatenate([y[:n_edge], y[-n_edge:]])
    try:
        coeffs = np.polyfit(x_edge, y_edge, degree)
    except Exception:
        coeffs = np.zeros(degree + 1)
    poly_coeffs = coeffs[::-1]  # numpy polyfit: highest-degree first → reverse to c0..cn
    return {f"bg_c{i}": float(poly_coeffs[i]) for i in range(degree + 1)}

# ── Model factory ──────────────────────────────────────────────────────────────

def make_lmfit_model(model_name: str):
    """Return an lmfit Model instance for the given model name."""
    if not LMFIT_AVAILABLE:
        raise RuntimeError("lmfit not installed — pip install lmfit")
    if model_name == "Gaussian":
        return lmfit.Model(_gaussian_fn)
    elif model_name == "Lorentzian":
        return lmfit.Model(_lorentzian_fn)
    elif model_name == "Voigt":
        return lmfit.models.VoigtModel()
    elif model_name == "Pseudo-Voigt":
        return lmfit.models.PseudoVoigtModel()
    elif model_name == "Super-Gaussian":
        return lmfit.Model(_supergaussian_fn)
    elif model_name == "Step (erf)":
        return lmfit.Model(_erf_step_fn)
    elif model_name == "Step (tanh)":
        return lmfit.Model(_tanh_step_fn)
    elif model_name == "Step (arctan)":
        return lmfit.Model(_arctan_step_fn)
    elif model_name == "Step (logistic)":
        return lmfit.Model(_logistic_step_fn)
    else:
        raise ValueError(f"Unknown model: {model_name!r}")

# ── Auto-guess parameters ──────────────────────────────────────────────────────

def auto_guess(x, y, model_name: str, bg_name: str = "None"):
    """Return lmfit.Parameters with auto-estimated initial values and bounds."""
    if not LMFIT_AVAILABLE:
        raise RuntimeError("lmfit not installed — pip install lmfit")

    x     = np.asarray(x, dtype=float)
    y     = np.asarray(y, dtype=float)
    model = make_lmfit_model(model_name)

    if model_name == "Gaussian":
        amp0, cen0, sig0 = _guess_peak(x, y)
        params = model.make_params()
        params["amplitude"].set(value=amp0, min=-np.inf, max=np.inf)
        params["center"].set(value=cen0, min=-np.inf, max=np.inf)
        params["sigma"].set(value=max(abs(sig0), 1e-12), min=1e-12, max=np.inf)
        params.add("fwhm", expr="2.3548 * sigma", vary=False)

    elif model_name == "Lorentzian":
        amp0, cen0, sig0 = _guess_peak(x, y)
        params = model.make_params()
        params["amplitude"].set(value=amp0, min=-np.inf, max=np.inf)
        params["center"].set(value=cen0, min=-np.inf, max=np.inf)
        params["sigma"].set(value=max(abs(sig0), 1e-12), min=1e-12, max=np.inf)
        params.add("fwhm", expr="2.0 * sigma", vary=False)

    elif model_name == "Voigt":
        amp0, cen0, sig0 = _guess_peak(x, y)
        params = model.make_params()
        params["amplitude"].set(
            value=amp0 * max(abs(sig0), 1e-12) * np.sqrt(2.0 * np.pi),
            min=-np.inf, max=np.inf,
        )
        params["center"].set(value=cen0, min=-np.inf, max=np.inf)
        params["sigma"].set(value=max(abs(sig0), 1e-12), min=1e-12, max=np.inf)
        params.add(
            "fwhm",
            expr="0.5346*2*sigma + sqrt(0.2166*(2*sigma)**2 + (2.3548*sigma)**2)",
            vary=False,
        )

    elif model_name == "Pseudo-Voigt":
        amp0, cen0, sig0 = _guess_peak(x, y)
        params = model.make_params()
        params["amplitude"].set(
            value=amp0 * max(abs(sig0), 1e-12) * np.sqrt(2.0 * np.pi),
            min=-np.inf, max=np.inf,
        )
        params["center"].set(value=cen0, min=-np.inf, max=np.inf)
        params["sigma"].set(value=max(abs(sig0), 1e-12), min=1e-12, max=np.inf)
        params["fraction"].set(value=0.5, min=0.0, max=1.0)
        params.add("fwhm", expr="2.0 * sigma", vary=False)

    elif model_name == "Super-Gaussian":
        amp0, cen0, sig0 = _guess_peak(x, y)
        params = model.make_params()
        params["amplitude"].set(value=amp0, min=-np.inf, max=np.inf)
        params["center"].set(value=cen0, min=-np.inf, max=np.inf)
        params["sigma"].set(value=max(abs(sig0), 1e-12), min=1e-12, max=np.inf)
        params["exponent"].set(value=1.5, min=0.5, max=50.0)
        params.add(
            "fwhm",
            expr="2.0 * sigma * (2.0 * log(2.0))**(1.0/(2.0*exponent))",
            vary=False,
        )

    elif model_name == "Step (erf)":
        amp0, cen0, sig0 = _guess_step(x, y)
        params = model.make_params()
        params["amplitude"].set(value=amp0, min=-np.inf, max=np.inf)
        params["center"].set(value=cen0, min=-np.inf, max=np.inf)
        params["sigma"].set(value=sig0 if abs(sig0) > 1e-12 else 1e-12,
                            min=-np.inf, max=np.inf)
        params.add("width_1090", expr="2.197 * abs(sigma)", vary=False)

    elif model_name == "Step (tanh)":
        amp0, cen0, sig0 = _guess_step(x, y)
        params = model.make_params()
        params["amplitude"].set(value=amp0, min=-np.inf, max=np.inf)
        params["center"].set(value=cen0, min=-np.inf, max=np.inf)
        params["sigma"].set(value=sig0 * 2 if abs(sig0) > 1e-12 else 1e-12,
                            min=-np.inf, max=np.inf)
        params.add("width_1090", expr="2.197 * abs(sigma)", vary=False)

    elif model_name == "Step (arctan)":
        amp0, cen0, sig0 = _guess_step(x, y)
        params = model.make_params()
        params["amplitude"].set(value=amp0, min=-np.inf, max=np.inf)
        params["center"].set(value=cen0, min=-np.inf, max=np.inf)
        params["sigma"].set(value=sig0 if abs(sig0) > 1e-12 else 1e-12,
                            min=-np.inf, max=np.inf)
        params.add("width_1090", expr="3.1416 * abs(sigma) * 0.8", vary=False)

    elif model_name == "Step (logistic)":
        amp0, cen0, sig0 = _guess_step(x, y)
        params = model.make_params()
        params["amplitude"].set(value=amp0, min=-np.inf, max=np.inf)
        params["center"].set(value=cen0, min=-np.inf, max=np.inf)
        params["sigma"].set(value=sig0 if abs(sig0) > 1e-12 else 1e-12,
                            min=-np.inf, max=np.inf)
        params.add("width_1090", expr="2.197 * abs(sigma)", vary=False)

    else:
        raise ValueError(f"Unknown model: {model_name!r}")

    # Merge background polynomial parameters if requested
    if bg_name != "None":
        bg_model = make_background_model(bg_name)
        bg_params = bg_model.make_params()
        bg_guess  = _guess_background(x, y, bg_name)
        for pname, val in bg_guess.items():
            if pname in bg_params:
                bg_params[pname].set(value=val, min=-np.inf, max=np.inf)
        params.update(bg_params)

    return params

# ── Main fitting entry point ───────────────────────────────────────────────────

def run_fit(x, y, params, model_name, method="leastsq", bg_name="None"):
    """Fit model to data. Returns (x_fit, y_fit, info_dict)."""
    if not LMFIT_AVAILABLE:
        raise RuntimeError("lmfit not installed")
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    mask = np.isfinite(x) & np.isfinite(y)
    x, y = x[mask], y[mask]
    if len(x) < 4:
        raise ValueError(f"Need ≥4 finite points, got {len(x)}")

    signal_model = make_lmfit_model(model_name)
    if bg_name != "None":
        bg_model = make_background_model(bg_name)
        model    = signal_model + bg_model
    else:
        model = signal_model
    result = model.fit(y, params, x=x, method=method, nan_policy="omit")

    x_fit = np.linspace(float(x[0]), float(x[-1]), max(500, len(x) * 5))
    y_fit = result.eval(x=x_fit)

    # Parameter names and values (skip derived/expr params)
    pnames      = [n for n, p in result.params.items() if not p.expr]
    param_names = [_PARAM_LABEL.get(n, n) for n in pnames]
    pvals       = [float(result.params[n].value)         for n in pnames]
    perrs       = [float(result.params[n].stderr or 0.0) for n in pnames]

    # FWHM or 10-90% width from derived parameter if present
    is_step = model_name.startswith("Step")
    if is_step and "width_1090" in result.params:
        fwhm = float(result.params["width_1090"].value)
    elif not is_step and "fwhm" in result.params:
        fwhm = float(result.params["fwhm"].value)
    else:
        fwhm = float("nan")

    # Center and annotation position
    center_val = float(
        result.params.get("center", list(result.params.values())[0]).value
    )
    if is_step:
        amp_val = float(result.params["amplitude"].value)
        ann_y   = amp_val / 2.0
    else:
        ann_y = float(result.eval(x=np.array([center_val]))[0])

    # R²
    y_pred = result.best_fit
    ss_res = float(np.sum((y - y_pred) ** 2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    r2     = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    info = {
        "model":        model_name,
        "x0":           center_val,
        "A":            ann_y,
        "fwhm":         fwhm,
        "r2":           r2,
        "params":       pvals,
        "perr":         perrs,
        "param_names":  param_names,
        "n_points":     int(len(x)),
        "result":       result,
    }
    return x_fit, y_fit, info

# ── Legacy compatibility ───────────────────────────────────────────────────────

def fit_peak(x, y, model="Gaussian"):
    """Legacy entry point — calls auto_guess + run_fit with lmfit.

    Raises RuntimeError with install hint if lmfit is unavailable.
    """
    if not LMFIT_AVAILABLE:
        raise RuntimeError(
            "lmfit is required for peak fitting — pip install lmfit"
        )
    params = auto_guess(x, y, model)
    return run_fit(x, y, params, model, method="leastsq")
