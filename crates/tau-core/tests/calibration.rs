//! The calibration numerics (D26 section 6) as properties: the fit recovers
//! known weights, where the spans say nothing a prediction is the bytes
//! (never zero), folding loses nothing whatever the order, the bound covers
//! what it says it covers, a jump is caught and noise is not, and a changed
//! marker resets.

use std::collections::BTreeMap;

use proptest::prelude::*;
use tau_core::calibration::{
    CLASSES, DECAY, DIM, Event, Features, Key, Observation, READY, ROLES, Record, SMALL, WARMUP,
    WEIGHTS, features, fold, predict,
};
use tau_core::tree::{Author, Body, Outcome, Part, Stop};

const WORDS: usize = 6;

fn key() -> Key {
    Key {
        provider: "openai".into(),
        base_url: Some("http://host/v1".into()),
        model: "m".into(),
    }
}

fn observation(span: Option<(Features, u64)>, markers: &[(&str, &str)]) -> Observation {
    Observation {
        markers: markers
            .iter()
            .map(|(k, v)| ((*k).to_string(), (*v).to_string()))
            .collect::<BTreeMap<_, _>>(),
        span,
        time: "t".into(),
    }
}

fn spanned(x: &Features, cost: f64) -> Observation {
    #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
    observation(Some((x.clone(), cost.round().max(0.0) as u64)), &[])
}

/// The cost a known model gives a row: its weights, and constants per node.
fn truth(w: &[f64], c: &[f64], x: &Features) -> f64 {
    let text: f64 = w.iter().zip(&x.0).map(|(a, b)| a * b).sum();
    let nodes: f64 = c.iter().zip(&x.0[WEIGHTS..]).map(|(a, b)| a * b).sum();
    text + nodes
}

/// A span's row: two of the roles, each with every class in multiples of 8
/// (so a weight in eighths gives a whole cost), and one node of each.
fn row() -> impl Strategy<Value = Features> {
    (
        0..ROLES,
        0..ROLES,
        prop::collection::vec(0u32..400, 2 * CLASSES),
    )
        .prop_map(|(a, b, counts)| {
            let mut x = Features::zero();
            for (k, role) in [a, b].into_iter().enumerate() {
                for class in 0..CLASSES {
                    x.0[role * CLASSES + class] += f64::from(counts[k * CLASSES + class] * 8);
                }
                x.0[WEIGHTS + role] += 1.0;
            }
            x
        })
}

/// Weights in eighths of a token, between 1/8 and 12/8.
fn weights() -> impl Strategy<Value = Vec<f64>> {
    prop::collection::vec((1u32..=12).prop_map(|k| f64::from(k) / 8.0), WEIGHTS)
}

fn learn(rows: &[Features], cost: impl Fn(usize, &Features) -> f64) -> (Record, Vec<Event>) {
    let mut record = Record::new(key());
    let mut events = vec![];
    for (i, x) in rows.iter().enumerate() {
        let (next, more) = fold(record, &spanned(x, cost(i, x)));
        record = next;
        events.extend(more);
    }
    (record, events)
}

/// A row of prose in one role: lowercase, spaces and words in proportion.
fn prose(role: usize, words: f64) -> Features {
    let mut x = Features::zero();
    x.0[role * CLASSES] = words * 5.0;
    x.0[role * CLASSES + 4] = words;
    x.0[role * CLASSES + WORDS] = words;
    x.0[WEIGHTS + role] = 1.0;
    x
}

proptest! {
    // Each case fits a 42-weight model 400 times: few cases, each a different model.
    #![proptest_config(ProptestConfig::with_cases(6))]

    /// D26 T3: synthetic spans from known weights recover those weights.
    #[test]
    fn spans_from_known_weights_recover_them(
        w in weights(),
        rows in prop::collection::vec(row(), 400),
    ) {
        let none = [0.0; ROLES];
        let (record, events) = learn(&rows, |_, x| truth(&w, &none, x));
        prop_assert_eq!(events, vec![]);
        for (got, want) in record.heuristic.weights.iter().zip(&w) {
            prop_assert!((got - want).abs() < 0.01, "weight {} for {}", got, want);
        }
        prop_assert_eq!(&record.heuristic.per_message, &vec![0; ROLES]);
    }

    /// With per-node constants in the truth as well, a new span is still
    /// predicted to within 2%: the weights take up what the constants do
    /// not, and the count is what matters.
    #[test]
    fn a_model_with_constants_predicts_new_spans(
        w in weights(),
        c in prop::collection::vec((0u32..12).prop_map(f64::from), ROLES),
        rows in prop::collection::vec(row(), 400),
        fresh in prop::collection::vec(row(), 20),
    ) {
        let (record, _) = learn(&rows, |_, x| truth(&w, &c, x));
        for x in &fresh {
            let want = truth(&w, &c, x);
            let got = predict(&record, x).unwrap();
            #[allow(clippy::cast_precision_loss)]
            let off = (got.tokens as f64 - want).abs();
            prop_assert!(off <= 0.02 * want + 2.0, "{} for {}", got.tokens, want);
        }
    }

}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(24))]

    /// Where the spans say nothing, a prediction is the text's bytes: a role
    /// or a class never seen is over-counted, never counted as nothing.
    #[test]
    fn what_was_never_seen_is_counted_by_its_bytes(
        sizes in prop::collection::vec(20.0f64..400.0, 40),
        unseen in 100.0f64..50_000.0,
    ) {
        // Only user and assistant prose is ever seen, at a quarter token a character.
        let rows: Vec<Features> = sizes
            .iter()
            .map(|n| {
                let mut x = prose(1, *n);
                x.add(&prose(2, *n));
                x
            })
            .collect();
        let (record, _) = learn(&rows, |_, x| {
            (x.0.iter().take(WEIGHTS).sum::<f64>() / 4.0).round()
        });
        // A tool result of digits: a role and a class the spans never held.
        let mut digits = Features::zero();
        digits.0[4 * CLASSES + 2] = unseen;
        digits.0[WEIGHTS + 4] = 1.0;
        let got = predict(&record, &digits).unwrap();
        #[allow(clippy::cast_precision_loss)]
        let tokens = got.tokens as f64;
        prop_assert!(tokens >= 0.95 * unseen, "{} tokens for {} bytes", tokens, unseen);
        // Digits in a role that was seen, where no digit ever was.
        let mut seen_role = Features::zero();
        seen_role.0[CLASSES + 2] = unseen;
        seen_role.0[WEIGHTS + 1] = 1.0;
        let got = predict(&record, &seen_role).unwrap();
        #[allow(clippy::cast_precision_loss)]
        let tokens = got.tokens as f64;
        prop_assert!(tokens >= 0.95 * unseen, "{} tokens for {} bytes", tokens, unseen);
    }

    /// Nothing is lost, whatever the order: every span is in the sums with at
    /// least the weight `DECAY^n` and at most 1, and the counts are exact. So
    /// two processes folding into one record, in whatever order they take
    /// its lock, lose neither's spans. (Decay makes the order matter for the
    /// weights: the newer span counts a little more, which is what decay is.)
    #[test]
    fn folding_loses_nothing_in_any_order(
        rows in prop::collection::vec(row(), 1..30),
        seed in any::<u64>(),
    ) {
        let cost = |x: &Features| x.0.iter().sum::<f64>();
        let mut shuffled: Vec<Features> = rows.clone();
        let mut state = seed | 1;
        for i in (1..shuffled.len()).rev() {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            #[allow(clippy::cast_possible_truncation)]
            shuffled.swap(i, (state % (i as u64 + 1)) as usize);
        }
        let (a, _) = learn(&rows, |_, x| cost(x));
        let (b, _) = learn(&shuffled, |_, x| cost(x));
        let n = rows.len();
        #[allow(clippy::cast_possible_truncation, clippy::cast_possible_wrap)]
        let least = DECAY.powi(n as i32);
        for record in [&a, &b] {
            prop_assert_eq!(record.observations, n as u64);
            prop_assert_eq!(record.spans, n as u64);
            let mut at = 0;
            for i in 0..DIM {
                for j in i..DIM {
                    let whole: f64 = rows.iter().map(|x| x.0[i] * x.0[j]).sum();
                    let kept = record.heuristic.xtx[at];
                    prop_assert!(kept <= whole * (1.0 + 1e-9) + 1e-9);
                    prop_assert!(kept >= whole * least * (1.0 - 1e-9) - 1e-9);
                    at += 1;
                }
            }
        }
        prop_assert!((a.heuristic.n - b.heuristic.n).abs() < 1e-9, "the weight of n spans");
    }

    /// D26 section 6.4: the bound covers what it says. With bounded noise it
    /// covers at least 95% of the record's own residuals, by its definition,
    /// and three in four of the spans that come next (in practice nearly all;
    /// the slack is for a small sample's luck).
    #[test]
    fn the_bound_covers_the_errors_it_was_made_from_and_those_that_follow(
        sizes in prop::collection::vec(50.0f64..3000.0, 160),
        noise in prop::collection::vec(-0.1f64..0.1, 160),
    ) {
        let rows: Vec<Features> = sizes.iter().map(|n| prose(4, *n)).collect();
        let cost = |i: usize, x: &Features| {
            (x.0.iter().take(WEIGHTS).sum::<f64>() / 4.0 * (1.0 + noise[i])).round()
        };
        let (record, events) = learn(&rows[..60], cost);
        prop_assert_eq!(events, vec![]);
        let q = record.error().unwrap();
        let held = &record.heuristic.residuals;
        let within = held.iter().filter(|r| r.0.abs() <= q).count();
        prop_assert!(within * 100 >= held.len() * 95);
        let mut record = record;
        let mut covered = 0;
        for (i, x) in rows.iter().enumerate().skip(60) {
            let got = predict(&record, x).unwrap();
            let actual = cost(i, x);
            #[allow(clippy::cast_precision_loss)]
            if (actual - got.tokens as f64).abs() <= got.bound as f64 {
                covered += 1;
            }
            record = fold(record, &spanned(x, actual)).0;
        }
        prop_assert!(covered >= 75, "{} of 100 covered", covered);
    }

    /// D26 section 6.3: noise is not a jump. Costs within 5% of the truth,
    /// in any pattern, never reset the record.
    #[test]
    fn noise_is_not_a_jump(
        sizes in prop::collection::vec(50.0f64..3000.0, 300),
        noise in prop::collection::vec(-0.05f64..0.05, 300),
    ) {
        let rows: Vec<Features> = sizes.iter().map(|n| prose(4, *n)).collect();
        let (record, events) = learn(&rows, |i, x| {
            (x.0.iter().take(WEIGHTS).sum::<f64>() / 4.0 * (1.0 + noise[i])).round()
        });
        prop_assert_eq!(events, vec![]);
        prop_assert!(record.ready());
    }

    /// D26 section 6.3: a step change is a jump. When the same text starts
    /// to cost 40% more (or less), the record says so within 15 spans, and
    /// starts again.
    #[test]
    fn a_step_change_is_caught(
        sizes in prop::collection::vec(200.0f64..3000.0, 100),
        noise in prop::collection::vec(-0.03f64..0.03, 100),
        up in any::<bool>(),
    ) {
        let rows: Vec<Features> = sizes.iter().map(|n| prose(4, *n)).collect();
        let step = if up { 1.4 } else { 0.6 };
        let (_, events) = learn(&rows, |i, x| {
            let rate = if i < 80 { 0.25 } else { 0.25 * step };
            (x.0.iter().take(WEIGHTS).sum::<f64>() * rate * (1.0 + noise[i])).round()
        });
        let jumps: Vec<&Event> = events.iter().filter(|e| matches!(e, Event::Jump { .. })).collect();
        prop_assert!(!jumps.is_empty(), "no jump in 20 spans after the step");
        if let Event::Jump { by } = jumps[0] {
            prop_assert_eq!(*by > 0.0, up);
        }
    }
}

#[test]
fn a_record_answers_only_once_it_is_ready() {
    let x = prose(4, 100.0);
    let mut record = Record::new(key());
    assert!(!record.ready());
    assert_eq!(predict(&record, &x), None);
    let needed = WARMUP as usize + READY;
    for i in 0..needed {
        assert_eq!(predict(&record, &x), None, "after {i} spans");
        assert_eq!(record.error(), None);
        record = fold(record, &spanned(&x, 150.0)).0;
    }
    assert!(record.ready());
    let got = predict(&record, &x).unwrap();
    assert!((149..=151).contains(&got.tokens), "{got:?}");
    assert!(got.bound >= 2);
    assert_eq!(
        predict(&record, &Features::zero()),
        None,
        "no node, no count"
    );
}

#[test]
fn a_reply_with_no_span_is_counted_and_teaches_nothing() {
    let record = Record::new(key());
    let (record, events) = fold(record, &observation(None, &[("model", "m-1")]));
    assert_eq!(events, vec![]);
    assert_eq!((record.observations, record.spans), (1, 0));
    assert_eq!(record.markers["model"], "m-1");
    assert_eq!(record.updated, "t");
}

#[test]
fn a_changed_marker_resets_and_a_new_or_missing_one_does_not() {
    let x = prose(4, 100.0);
    let mut record = Record::new(key());
    for _ in 0..30 {
        let seen = Some((x.clone(), 150));
        record = fold(record, &observation(seen, &[("system_fingerprint", "fp1")])).0;
    }
    assert!(record.ready());
    let before = record.heuristic.n;
    // A marker the record never had says nothing; nor does one that is not sent.
    let (record, events) = fold(record, &observation(None, &[("build_info", "b1")]));
    assert_eq!(events, vec![]);
    let (record, events) = fold(record, &observation(None, &[]));
    assert_eq!(events, vec![]);
    assert!(record.ready());
    let (record, events) = fold(record, &observation(None, &[("system_fingerprint", "fp2")]));
    assert_eq!(
        events,
        vec![Event::Marker {
            name: "system_fingerprint".into(),
            was: "fp1".into(),
            now: "fp2".into()
        }]
    );
    assert!(!record.ready(), "the residuals are gone");
    assert_eq!(predict(&record, &x), None);
    assert!(
        record.heuristic.n < before * 0.06,
        "the sums keep 5% of their weight"
    );
    assert_eq!(record.markers["system_fingerprint"], "fp2");
    assert_eq!(record.spans, 30, "what was seen is still counted");
}

#[test]
fn the_bound_narrows_as_residuals_gather() {
    let rows: Vec<Features> = (0..120).map(|i| prose(4, 100.0 + f64::from(i))).collect();
    let mut record = Record::new(key());
    let probe = prose(4, 1000.0);
    let mut bounds = vec![];
    for (i, x) in rows.iter().enumerate() {
        // Costs alternate 2% above and below a quarter token a character.
        let rate = if i % 2 == 0 { 0.255 } else { 0.245 };
        let cost = (x.0.iter().take(WEIGHTS).sum::<f64>() * rate).round();
        record = fold(record, &spanned(x, cost)).0;
        if let Some(got) = predict(&record, &probe) {
            bounds.push(got.bound);
        }
    }
    assert!(bounds.len() > 100);
    let (early, late) = (bounds[SMALL], bounds[bounds.len() - 1]);
    assert!(late < early, "{early} then {late}");
    assert!(
        late <= 1750 * 4 / 100,
        "about 2% of 1,750 tokens, and its margin: {late}"
    );
}

#[test]
#[allow(
    clippy::float_cmp,
    reason = "a feature is a count or a 0/1 flag, exact in f64; equality is the claim"
)]
fn the_features_of_a_body_are_its_text_by_role_and_class() {
    let user = features(&Body::User("Héllo wörld 42!".into()));
    let at = CLASSES; // the user role
    assert_eq!(
        &user.0[at..at + CLASSES],
        [7.0, 1.0, 2.0, 1.0, 2.0, 4.0, 3.0]
    );
    assert_eq!(user.0[WEIGHTS + 1], 1.0);
    assert_eq!(
        user.0.iter().take(WEIGHTS).sum::<f64>() - 3.0,
        17.0,
        "its 17 bytes, and 3 words"
    );

    let batch = Body::Assistant {
        parts: vec![
            Part::thinking("hm"),
            Part::text("ok"),
            Part::Call {
                id: "c1".into(),
                tool: "read".into(),
                args_raw: "{}".into(),
                replay: None,
            },
        ],
        stop: Stop::Tools,
        finish_reason: None,
        meta: None,
    };
    let x = features(&batch);
    assert_eq!(
        x.0[WEIGHTS + 3],
        1.0,
        "a reply with a call has the role calls"
    );
    assert_eq!(x.0[WEIGHTS + 2], 0.0);
    assert_eq!(x.0[WEIGHTS + 5], 1.0, "and its thinking is counted apart");
    assert_eq!(x.0[5 * CLASSES], 2.0, "hm");
    assert_eq!(x.0[3 * CLASSES], 7.0, "ok, c, read");

    let results = Body::Results(vec![
        ("c1".into(), Outcome::Ok("fine".into()), Author::Tools),
        ("c2".into(), Outcome::Interrupted, Author::Tools),
    ]);
    let x = features(&results);
    assert_eq!(x.0[WEIGHTS + 4], 1.0);
    assert_eq!(x.0[4 * CLASSES + WORDS], 4.0, "c1, fine, c2, Interrupted");

    assert!(
        features(&Body::Custom {
            kind: "k".into(),
            data: "d".into()
        })
        .is_empty()
    );
    let mut sum = features(&Body::User("a".into()));
    sum.add(&features(&Body::System("b".into())));
    assert_eq!((sum.0[WEIGHTS], sum.0[WEIGHTS + 1]), (1.0, 1.0));
}

#[test]
fn a_record_is_json_and_reads_back_as_it_was() {
    let x = prose(4, 100.0);
    let mut record = Record::new(key());
    for _ in 0..12 {
        record = fold(
            record,
            &observation(Some((x.clone(), 150)), &[("model", "m")]),
        )
        .0;
    }
    let text = serde_json::to_string(&record).unwrap();
    let back: Record = serde_json::from_str(&text).unwrap();
    assert!(back.well_formed());
    assert_eq!((back.observations, back.spans), (12, 12));
    assert_eq!(back.heuristic.per_message, record.heuristic.per_message);
    let x = prose(4, 300.0);
    assert_eq!(
        predict(&back, &x),
        predict(&record, &x),
        "it counts as it did"
    );
    assert!(text.contains("\"tokenizer\":null"));
}
