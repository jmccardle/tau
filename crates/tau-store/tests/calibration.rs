//! The calibration store (D26 section 6.5): a record survives the disk, two
//! stores folding into one file lose nothing, and a file that is not a
//! record this build reads is left alone.

use std::collections::BTreeMap;
use std::fs;
use std::sync::Arc;
use std::thread;

use proptest::prelude::*;
use tau_core::calibration::{CLASSES, Features, Key, Observation, Record, WEIGHTS, fold};
use tau_store::calibration::{CalibrationStore, Error};

const NAME: &str = "openai/0123456789abcdef.json";

fn key() -> Key {
    Key {
        provider: "openai".into(),
        base_url: Some("http://host/v1".into()),
        model: "m".into(),
    }
}

/// Whether two records are the same but for the last digit of a sum: a number written as
/// JSON and read back may differ from itself in its last bit.
fn same(a: &Record, b: &Record) -> bool {
    let close = |x: &[f64], y: &[f64]| {
        x.len() == y.len()
            && x.iter()
                .zip(y)
                .all(|(p, q)| (p - q).abs() <= 1e-9 * p.abs().max(q.abs()).max(1.0))
    };
    let (g, h) = (&a.heuristic, &b.heuristic);
    let flat = |r: &[(f64, f64)]| r.iter().flat_map(|p| [p.0, p.1]).collect::<Vec<_>>();
    (
        a.version,
        &a.key,
        &a.markers,
        a.observations,
        a.spans,
        &a.updated,
    ) == (
        b.version,
        &b.key,
        &b.markers,
        b.observations,
        b.spans,
        &b.updated,
    ) && (g.since_reset, &g.per_message) == (h.since_reset, &h.per_message)
        && close(&g.xtx, &h.xtx)
        && close(&g.xty, &h.xty)
        && close(&g.weights, &h.weights)
        && close(&[g.n, g.cusum.0, g.cusum.1], &[h.n, h.cusum.0, h.cusum.1])
        && close(&flat(&g.residuals), &flat(&h.residuals))
}

fn span(words: u32) -> Observation {
    let mut x = Features::zero();
    x.0[4 * CLASSES] = f64::from(words) * 5.0;
    x.0[4 * CLASSES + 4] = f64::from(words);
    x.0[WEIGHTS + 4] = 1.0;
    Observation {
        markers: BTreeMap::from([("model".to_string(), "m-1".to_string())]),
        span: Some((x, u64::from(words) * 3 / 2)),
        time: "2026-11-10T00:00:00Z".into(),
    }
}

#[test]
fn making_a_store_and_reading_from_it_touch_nothing() {
    let home = tempfile::tempdir().unwrap();
    let dir = home.path().join("calibration");
    let store = CalibrationStore::new(&dir);
    assert!(store.read(NAME).unwrap().is_none());
    assert_eq!(store.names().unwrap(), Vec::<String>::new());
    assert!(!store.remove(NAME).unwrap());
    assert!(!dir.exists(), "nothing is created until an observation");
}

#[test]
fn an_observation_is_folded_written_and_read_back() {
    let home = tempfile::tempdir().unwrap();
    let store = CalibrationStore::new(home.path().join("calibration"));
    let (first, events) = store.observe(NAME, &key(), &span(100)).unwrap();
    assert_eq!(events, vec![]);
    assert_eq!((first.observations, first.spans), (1, 1));
    let (second, _) = store.observe(NAME, &key(), &span(200)).unwrap();
    assert_eq!(second.observations, 2);
    assert!(same(&store.read(NAME).unwrap().unwrap(), &second));
    let by_hand = fold(fold(Record::new(key()), &span(100)).0, &span(200)).0;
    assert!(
        same(&second, &by_hand),
        "the store adds nothing to the fold"
    );
    assert_eq!(store.names().unwrap(), [NAME]);
    assert!(store.remove(NAME).unwrap());
    assert!(store.read(NAME).unwrap().is_none());
    assert!(!store.remove(NAME).unwrap());
}

#[cfg(unix)]
#[test]
fn a_record_is_readable_by_its_owner_only() {
    use std::os::unix::fs::PermissionsExt;
    let home = tempfile::tempdir().unwrap();
    let store = CalibrationStore::new(home.path());
    store.observe(NAME, &key(), &span(10)).unwrap();
    let mode = fs::metadata(home.path().join(NAME))
        .unwrap()
        .permissions()
        .mode();
    assert_eq!(mode & 0o777, 0o600);
}

#[test]
fn a_record_from_a_newer_tau_is_left_alone_and_not_used() {
    let home = tempfile::tempdir().unwrap();
    let store = CalibrationStore::new(home.path());
    store.observe(NAME, &key(), &span(10)).unwrap();
    let path = home.path().join(NAME);
    let newer = fs::read_to_string(&path)
        .unwrap()
        .replacen("\"version\":1", "\"version\":2", 1);
    fs::write(&path, &newer).unwrap();
    assert!(matches!(
        store.read(NAME),
        Err(Error::Newer { version: 2, .. })
    ));
    assert!(matches!(
        store.observe(NAME, &key(), &span(10)),
        Err(Error::Newer { .. })
    ));
    assert_eq!(fs::read_to_string(&path).unwrap(), newer, "not overwritten");
}

#[test]
fn a_file_that_is_not_a_record_is_damaged_and_never_overwritten() {
    let home = tempfile::tempdir().unwrap();
    let store = CalibrationStore::new(home.path());
    store.observe(NAME, &key(), &span(10)).unwrap();
    let path = home.path().join(NAME);
    let good = fs::read_to_string(&path).unwrap();
    let cut = &good[..good.len() / 2];
    let no_version = good.replacen("\"version\":1", "\"version\":\"one\"", 1);
    let short = good.replacen("\"xty\":[", "\"xty\":[1.0,", 1);
    for bad in [cut, "[]", &no_version, &short] {
        fs::write(&path, bad).unwrap();
        assert!(
            matches!(store.read(NAME), Err(Error::Damaged { .. })),
            "{bad:.40}"
        );
        assert!(matches!(
            store.observe(NAME, &key(), &span(10)),
            Err(Error::Damaged { .. })
        ));
        assert_eq!(fs::read_to_string(&path).unwrap(), bad, "left as it was");
    }
    assert!(
        store.remove(NAME).unwrap(),
        "removing it is how it is repaired"
    );
    assert_eq!(
        store
            .observe(NAME, &key(), &span(10))
            .unwrap()
            .0
            .observations,
        1
    );
}

#[test]
fn a_record_of_another_model_under_the_name_is_not_folded_into() {
    let home = tempfile::tempdir().unwrap();
    let store = CalibrationStore::new(home.path());
    store.observe(NAME, &key(), &span(10)).unwrap();
    let other = Key {
        model: "other".into(),
        ..key()
    };
    assert!(matches!(
        store.observe(NAME, &other, &span(10)),
        Err(Error::Damaged { .. })
    ));
}

#[test]
fn a_name_that_leaves_the_directory_is_refused() {
    let home = tempfile::tempdir().unwrap();
    let store = CalibrationStore::new(home.path().join("calibration"));
    for bad in [
        "../x.json",
        "/etc/x.json",
        "openai/x.txt",
        "openai/../../x.json",
        "",
    ] {
        assert!(matches!(store.read(bad), Err(Error::BadName(_))), "{bad}");
        assert!(matches!(
            store.observe(bad, &key(), &span(1)),
            Err(Error::BadName(_))
        ));
    }
    assert!(!home.path().join("calibration").exists());
}

#[test]
fn two_stores_folding_at_once_lose_nothing() {
    let home = tempfile::tempdir().unwrap();
    let dir = Arc::new(home.path().join("calibration"));
    let each = 40;
    let workers: Vec<_> = (0..4)
        .map(|w| {
            let dir = Arc::clone(&dir);
            thread::spawn(move || {
                let store = CalibrationStore::new(dir.as_path());
                for i in 0..each {
                    store.observe(NAME, &key(), &span(50 + w * 10 + i)).unwrap();
                }
            })
        })
        .collect();
    for worker in workers {
        worker.join().unwrap();
    }
    let record = CalibrationStore::new(dir.as_path())
        .read(NAME)
        .unwrap()
        .unwrap();
    assert_eq!(record.observations, u64::from(4 * each));
    assert_eq!(record.spans, u64::from(4 * each));
    assert!(record.well_formed());
    let left: Vec<_> = fs::read_dir(dir.join("openai"))
        .unwrap()
        .map(|e| e.unwrap().file_name().into_string().unwrap())
        .filter(|n| n.contains(".new-"))
        .collect();
    assert!(left.is_empty(), "no half-written file is left: {left:?}");
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(16))]

    /// Two stores on one file, taking turns in any order: the file ends as the
    /// same observations folded in that order in memory, each one counted.
    #[test]
    fn the_file_is_the_fold_of_what_was_observed_in_the_order_it_was(
        turns in prop::collection::vec((any::<bool>(), 1u32..500), 1..25),
    ) {
        let home = tempfile::tempdir().unwrap();
        let a = CalibrationStore::new(home.path());
        let b = CalibrationStore::new(home.path());
        let mut expected = Record::new(key());
        for (first, words) in &turns {
            let observation = span(*words);
            let store = if *first { &a } else { &b };
            store.observe(NAME, &key(), &observation).unwrap();
            expected = fold(expected, &observation).0;
        }
        let record = a.read(NAME).unwrap().unwrap();
        prop_assert_eq!(record.observations, turns.len() as u64);
        prop_assert!(same(&record, &expected));
    }
}
