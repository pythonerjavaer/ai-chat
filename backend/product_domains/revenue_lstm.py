"""Small CPU LSTM using the application's existing NumPy dependency.

Four standard gates, backpropagation through time, clipped gradients and Adam.
Avoids loading a second large ML runtime into the shared free web instance.
"""
from __future__ import annotations

import numpy as np


class RevenueLSTM:
    def __init__(self, hidden: int = 8, seed: int = 2026):
        self.hidden = hidden
        rng = np.random.default_rng(seed)
        self.params = {
            "w": rng.normal(0, 1 / np.sqrt(hidden + 1), (hidden + 1, 4 * hidden)),
            "b": np.zeros(4 * hidden),
            "out": rng.normal(0, 1 / np.sqrt(hidden), (hidden, 1)),
            "bias": np.zeros(1),
        }
        self.params["b"][hidden:2 * hidden] = 1.0

    def forward(self, x: np.ndarray):
        h = np.zeros((len(x), self.hidden))
        c = np.zeros_like(h)
        cache = []
        for index in range(x.shape[1]):
            joined = np.concatenate([x[:, index, :], h], axis=1)
            gates = joined @ self.params["w"] + self.params["b"]
            i, f, o = [1 / (1 + np.exp(-np.clip(part, -40, 40)))
                       for part in np.split(gates[:, :3 * self.hidden], 3, axis=1)]
            g = np.tanh(gates[:, 3 * self.hidden:])
            previous_c = c
            c = f * c + i * g
            h = o * np.tanh(c)
            cache.append((joined, i, f, o, g, previous_c, c))
        return h @ self.params["out"] + self.params["bias"], h, cache

    def loss_and_grad(self, x: np.ndarray, y: np.ndarray):
        prediction, h, cache = self.forward(x)
        residual = prediction - y
        loss = float(np.mean(residual ** 2))
        dy = 2 * residual / len(x)
        grads = {name: np.zeros_like(value) for name, value in self.params.items()}
        grads["out"] = h.T @ dy
        grads["bias"] = dy.sum(axis=0)
        dh = dy @ self.params["out"].T
        dc = np.zeros_like(h)
        for joined, i, f, o, g, previous_c, c in reversed(cache):
            tanh_c = np.tanh(c)
            total_dc = dc + dh * o * (1 - tanh_c ** 2)
            gates_grad = np.concatenate([
                total_dc * g * i * (1 - i),
                total_dc * previous_c * f * (1 - f),
                dh * tanh_c * o * (1 - o),
                total_dc * i * (1 - g ** 2),
            ], axis=1)
            grads["w"] += joined.T @ gates_grad
            grads["b"] += gates_grad.sum(axis=0)
            dh = (gates_grad @ self.params["w"].T)[:, 1:]
            dc = total_dc * f
        return loss, grads

    def fit(self, x: np.ndarray, y: np.ndarray, epochs: int = 60):
        first = {name: np.zeros_like(value) for name, value in self.params.items()}
        second = {name: np.zeros_like(value) for name, value in self.params.items()}
        for step in range(1, epochs + 1):
            loss, grads = self.loss_and_grad(x, y)
            if not np.isfinite(loss):
                raise ValueError("LSTM training produced a non-finite loss.")
            norm = max(1.0, float(np.sqrt(sum(np.sum(value ** 2) for value in grads.values()))) / 5)
            for name, value in self.params.items():
                gradient = grads[name] / norm
                first[name] = .9 * first[name] + .1 * gradient
                second[name] = .999 * second[name] + .001 * gradient ** 2
                value -= .01 * (first[name] / (1 - .9 ** step)) / (
                    np.sqrt(second[name] / (1 - .999 ** step)) + 1e-8)
        return self

    def predict(self, x: np.ndarray) -> np.ndarray:
        return self.forward(x)[0]


def fit_series(values: np.ndarray, *, lookback: int = 12):
    center, scale = float(np.mean(values)), max(float(np.std(values)), 1.0)
    normalized = (values - center) / scale
    windows = np.asarray([normalized[index-lookback:index] for index in range(lookback, len(values))])[:, :, None]
    targets = normalized[lookback:, None]
    model = RevenueLSTM().fit(windows, targets)
    return model, center, scale


def next_value(history: list[float], fitted) -> float:
    model, center, scale = fitted
    window = ((np.asarray(history[-12:]) - center) / scale).reshape(1, 12, 1)
    return max(0.0, float(model.predict(window)[0, 0]) * scale + center)
