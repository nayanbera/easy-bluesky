"""peak_fit.py — Peak-fitting models for the MongoDB Browser plot."""

import numpy as np

try:
    from scipy.optimize import curve_fit as _curve_fit
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False

MODELS = ["Gaussian", "Lorentzian", "Super-Gaussian"]

# ── Model functions ────────────────────────────────────────────────────────────

def _gaussian(x, A, x0, sigma):
    return A * np.exp(-0.5 * ((x - x0) / sigma) ** 2)

def _lorentzian(x, A, x0, gamma):
    return A / (1.0 + ((x - x0) / gamma) ** 2)

def _supergaussian(x, A, x0, sigma, n):
    return A * np.exp(-(((x - x0) ** 2) / (2.0 * sigma ** 2)) ** n)

# ── FWHM from fit parameters ───────────────────────────────────────────────────

def _fwhm_gaussian(sigma):
    return 2.0 * np.sqrt(2.0 * np.log(2.0)) * abs(sigma)

def _fwhm_lorentzian(gamma):
    return 2.0 * abs(gamma)

def _fwhm_supergaussian(sigma, n):
    # FWHM = 2 * sigma * sqrt(2 * ln(2)^(1/n))
    return 2.0 * abs(sigma) * np.sqrt(2.0 * np.log(2.0) ** (1.0 / n))

# ── Auto initial-guess ─────────────────────────────────────────────────────────

def _initial_guess(x, y):
    i_max = int(np.argmax(y))
    A = float(y[i_max])
    x0 = float(x[i_max])
    half = A * 0.5
    above = x[y >= half]
    if len(above) >= 2:
        hw = (float(above[-1]) - float(above[0])) / 2.0
    else:
        hw = (float(x[-1]) - float(x[0])) / 4.0
    step = abs(float(x[1] - x[0])) if len(x) > 1 else 1.0
    hw = max(hw, step)
    sigma = hw / np.sqrt(2.0 * np.log(2.0))
    gamma = hw
    return A, x0, sigma, gamma

# ── Main fitting entry point ───────────────────────────────────────────────────

def fit_peak(x, y, model="Gaussian"):
    """Fit a peak in *x*, *y* with the chosen *model*.

    Returns (x_fit, y_fit, info) where:
      x_fit  : dense x array for plotting the smooth fit curve
      y_fit  : model values on x_fit
      info   : dict — keys: model, A, x0, fwhm, r2, params, perr,
                      param_names, param_values, n_points
                      (plus n_exp for Super-Gaussian)

    Raises RuntimeError on convergence failure, ValueError on bad input.
    """
    if not SCIPY_AVAILABLE:
        raise RuntimeError(
            "scipy is required for peak fitting — pip install scipy"
        )

    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    mask = np.isfinite(x) & np.isfinite(y)
    x, y = x[mask], y[mask]
    if len(x) < 4:
        raise ValueError(f"Need at least 4 finite points to fit (got {len(x)})")

    A0, x0_0, sigma0, gamma0 = _initial_guess(x, y)

    try:
        if model == "Gaussian":
            popt, pcov = _curve_fit(
                _gaussian, x, y, p0=[A0, x0_0, sigma0], maxfev=20_000,
                bounds=([-np.inf, -np.inf, 1e-12], [np.inf, np.inf, np.inf]),
            )
            perr = np.sqrt(np.diag(pcov))
            A, x0, sigma = popt
            fwhm = _fwhm_gaussian(sigma)
            fn   = _gaussian
            param_names = ["Amplitude (A)", "Center (x₀)", "Std dev (σ)"]

        elif model == "Lorentzian":
            popt, pcov = _curve_fit(
                _lorentzian, x, y, p0=[A0, x0_0, gamma0], maxfev=20_000,
                bounds=([-np.inf, -np.inf, 1e-12], [np.inf, np.inf, np.inf]),
            )
            perr = np.sqrt(np.diag(pcov))
            A, x0, gamma = popt
            fwhm = _fwhm_lorentzian(gamma)
            fn   = _lorentzian
            param_names = ["Amplitude (A)", "Center (x₀)", "Half-width (γ)"]

        elif model == "Super-Gaussian":
            popt, pcov = _curve_fit(
                _supergaussian, x, y, p0=[A0, x0_0, sigma0, 1.5], maxfev=20_000,
                bounds=(
                    [0.0,      -np.inf, 1e-12, 0.5],
                    [np.inf,    np.inf, np.inf, 50.0],
                ),
            )
            perr = np.sqrt(np.diag(pcov))
            A, x0, sigma, n_exp = popt
            fwhm = _fwhm_supergaussian(sigma, n_exp)
            fn   = _supergaussian
            param_names = ["Amplitude (A)", "Center (x₀)", "Std dev (σ)", "Exponent (n)"]

        else:
            raise ValueError(f"Unknown model '{model}'")

    except RuntimeError as exc:
        raise RuntimeError(f"{model} fit did not converge: {exc}") from exc

    # R²
    y_pred = fn(x, *popt)
    ss_res = float(np.sum((y - y_pred) ** 2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    # Dense curve for smooth rendering
    x_fit = np.linspace(float(x[0]), float(x[-1]), max(500, len(x) * 5))
    y_fit = fn(x_fit, *popt)

    info = {
        "model":        model,
        "A":            float(A),
        "x0":           float(x0),
        "fwhm":         float(fwhm),
        "r2":           r2,
        "params":       list(map(float, popt)),
        "perr":         list(map(float, perr)),
        "param_names":  param_names,
        "n_points":     int(len(x)),
    }
    if model == "Super-Gaussian":
        info["n_exp"] = float(popt[3])

    return x_fit, y_fit, info
