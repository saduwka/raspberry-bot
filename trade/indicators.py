"""Индикаторы на чистом Python (без pandas/ta), совместимые с библиотекой ta."""


def _nan():
    return float("nan")


def sma(values, window):
    out = [_nan()] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= window:
            s -= values[i - window]
        if i >= window - 1:
            out[i] = s / window
    return out


def ema(values, window):
    """Как ta.EMAIndicator (pandas ewm adjust=False, min_periods=window)."""
    out = [_nan()] * len(values)
    if not values:
        return out
    alpha = 2.0 / (window + 1)
    cur = values[0]
    for i, v in enumerate(values):
        cur = v if i == 0 else alpha * v + (1 - alpha) * cur
        if i >= window - 1:
            out[i] = cur
    return out


def _wilder(values, window, start):
    """Сглаживание Уайлдера: первое значение — среднее, далее (prev*(n-1)+x)/n."""
    out = [_nan()] * len(values)
    if len(values) < start + window:
        return out
    cur = sum(values[start:start + window]) / window
    out[start + window - 1] = cur
    for i in range(start + window, len(values)):
        cur = (cur * (window - 1) + values[i]) / window
        out[i] = cur
    return out


def rsi(close, window=14):
    """Как ta.RSIIndicator (ewm alpha=1/n, adjust=False, min_periods=n)."""
    n = len(close)
    out = [_nan()] * n
    if n < 2:
        return out
    alpha = 1.0 / window
    up_avg = down_avg = None
    for i in range(1, n):
        d = close[i] - close[i - 1]
        up = d if d > 0 else 0.0
        dn = -d if d < 0 else 0.0
        if up_avg is None:
            up_avg, down_avg = up, dn
        else:
            up_avg = alpha * up + (1 - alpha) * up_avg
            down_avg = alpha * dn + (1 - alpha) * down_avg
        if i >= window:
            if down_avg == 0:
                out[i] = 100.0 if up_avg > 0 else 50.0
            else:
                rs = up_avg / down_avg
                out[i] = 100 - 100 / (1 + rs)
    return out


def true_range(high, low, close):
    tr = [high[0] - low[0]]
    for i in range(1, len(close)):
        tr.append(max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])))
    return tr


def atr(high, low, close, window=14):
    """Как ta.AverageTrueRange: первое значение — среднее TR[0..n-1]."""
    return _wilder(true_range(high, low, close), window, 0)


def adx(high, low, close, window=14):
    """ADX, +DI, -DI по Уайлдеру. Возвращает (adx, di_pos, di_neg)."""
    n = len(close)
    nan = [_nan()] * n
    if n < 2 * window + 1:
        return nan[:], nan[:], nan[:]
    tr = true_range(high, low, close)
    pdm = [0.0]
    ndm = [0.0]
    for i in range(1, n):
        up = high[i] - high[i - 1]
        dn = low[i - 1] - low[i]
        pdm.append(up if up > dn and up > 0 else 0.0)
        ndm.append(dn if dn > up and dn > 0 else 0.0)

    def smooth(arr):
        out = [_nan()] * n
        cur = sum(arr[1:window + 1])
        out[window] = cur
        for i in range(window + 1, n):
            cur = cur - cur / window + arr[i]
            out[i] = cur
        return out

    str_ = smooth(tr)
    sp = smooth(pdm)
    sn = smooth(ndm)
    dip = [_nan()] * n
    din = [_nan()] * n
    dx = [_nan()] * n
    for i in range(window, n):
        if str_[i] and str_[i] == str_[i]:
            dip[i] = 100 * sp[i] / str_[i]
            din[i] = 100 * sn[i] / str_[i]
            s = dip[i] + din[i]
            dx[i] = 100 * abs(dip[i] - din[i]) / s if s else 0.0
    adx_out = [_nan()] * n
    first = 2 * window - 1
    cur = sum(dx[window:first + 1]) / window
    adx_out[first] = cur
    for i in range(first + 1, n):
        cur = (cur * (window - 1) + dx[i]) / window
        adx_out[i] = cur
    return adx_out, dip, din
