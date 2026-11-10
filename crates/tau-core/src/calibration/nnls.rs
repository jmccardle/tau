//! Non-negative least squares on the normal equations (Lawson and Hanson's
//! active-set method): the `w >= 0` that minimizes `wᵀAw / 2 - bᵀw` for a
//! symmetric positive definite `A`. The fit hands it `XᵀX` plus a ridge, so
//! `A` is always positive definite. No dependency: the systems are at most
//! [`super::WEIGHTS`] wide, and a dense Cholesky solve is enough.

#![allow(clippy::many_single_char_names)] // the method's own names: A, b, w, s, L

/// The solution, `n` numbers, each at least 0. `a` is `n` by `n`, row by
/// row; `b` has `n` numbers. A variable the solve cannot use (its part of
/// `A` is not positive definite to working precision) is left at 0.
pub fn solve(a: &[f64], b: &[f64], n: usize) -> Vec<f64> {
    // Jacobi scaling: the columns are counts of characters and of nodes,
    // whose sizes differ by orders of magnitude. It keeps `w >= 0`.
    let d: Vec<f64> = (0..n).map(|i| 1.0 / a[i * n + i].sqrt()).collect();
    let sa: Vec<f64> = (0..n * n).map(|k| a[k] * d[k / n] * d[k % n]).collect();
    let sb: Vec<f64> = (0..n).map(|i| b[i] * d[i]).collect();
    let tolerance = 1e-10 * sb.iter().fold(1.0_f64, |m, v| m.max(v.abs()));
    let mut w = vec![0.0; n];
    let mut passive = vec![false; n];
    let mut barred = vec![false; n];
    // Each pass frees one variable. One that its own pass sends straight
    // back to 0, or that cannot be solved for, is barred, so the loop ends
    // however the arithmetic rounds.
    for _ in 0..3 * n {
        let Some(j) = steepest(&sa, &sb, &w, &passive, &barred, n, tolerance) else {
            break;
        };
        passive[j] = true;
        if !descend(&sa, &sb, &mut w, &mut passive, n) || !passive[j] {
            passive[j] = false;
            barred[j] = true;
        }
    }
    w.iter().zip(&d).map(|(v, s)| v * s).collect()
}

/// The variable held at 0 that most wants to rise, if any does.
fn steepest(
    a: &[f64],
    b: &[f64],
    w: &[f64],
    passive: &[bool],
    barred: &[bool],
    n: usize,
    tolerance: f64,
) -> Option<usize> {
    let mut best = None;
    let mut most = tolerance;
    for i in (0..n).filter(|i| !passive[*i] && !barred[*i]) {
        let pull = b[i] - (0..n).map(|k| a[i * n + k] * w[k]).sum::<f64>();
        if pull > most {
            most = pull;
            best = Some(i);
        }
    }
    best
}

/// Moves `w` to the least squares solution over the passive variables,
/// stopping at the boundary and dropping the variables that reach it, until
/// that solution is positive. False if the passive part of `A` cannot be
/// solved.
fn descend(a: &[f64], b: &[f64], w: &mut [f64], passive: &mut [bool], n: usize) -> bool {
    for _ in 0..=n {
        let free: Vec<usize> = (0..n).filter(|i| passive[*i]).collect();
        let Some(s) = cholesky(a, b, &free, n) else {
            return false;
        };
        if s.iter().all(|v| *v > 0.0) {
            for (i, v) in free.iter().zip(&s) {
                w[*i] = *v;
            }
            return true;
        }
        let step = free
            .iter()
            .zip(&s)
            .filter(|(_, v)| **v <= 0.0)
            .map(|(i, v)| w[*i] / (w[*i] - v))
            .fold(1.0_f64, f64::min);
        for (i, v) in free.iter().zip(&s) {
            w[*i] += step * (v - w[*i]);
            if w[*i] <= 1e-14 {
                w[*i] = 0.0;
                passive[*i] = false;
            }
        }
    }
    false
}

/// Solves `A[free, free] s = b[free]` by Cholesky; `None` if a pivot is not
/// positive.
fn cholesky(a: &[f64], b: &[f64], free: &[usize], n: usize) -> Option<Vec<f64>> {
    let m = free.len();
    let mut l = vec![0.0; m * m];
    for i in 0..m {
        for j in 0..=i {
            let dot: f64 = (0..j).map(|k| l[i * m + k] * l[j * m + k]).sum();
            let v = a[free[i] * n + free[j]] - dot;
            if i == j {
                if v <= 1e-12 {
                    return None;
                }
                l[i * m + i] = v.sqrt();
            } else {
                l[i * m + j] = v / l[j * m + j];
            }
        }
    }
    let mut y = vec![0.0; m];
    for i in 0..m {
        let dot: f64 = (0..i).map(|k| l[i * m + k] * y[k]).sum();
        y[i] = (b[free[i]] - dot) / l[i * m + i];
    }
    let mut s = vec![0.0; m];
    for i in (0..m).rev() {
        let dot: f64 = (i + 1..m).map(|k| l[k * m + i] * s[k]).sum();
        s[i] = (y[i] - dot) / l[i * m + i];
    }
    Some(s)
}
