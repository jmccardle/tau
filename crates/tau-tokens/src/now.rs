//! The date and time a template's `strftime_now` prints. This crate reads no clock: the caller
//! says what time it is, and the formatting is C's in the "C" locale, which is what Python's
//! `datetime.now().strftime(format)` gives on a server that never set a locale.

use std::fmt::Write as _;

use crate::error::Error;

const MONTHS: [&str; 12] = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
];
const DAYS: [&str; 7] = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
];
const BEFORE_MONTH: [u32; 12] = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334];

/// A local date and time with no zone, as a server's `datetime.now()` is.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Now {
    year: i32,
    month: u32,
    day: u32,
    hour: u32,
    minute: u32,
    second: u32,
}

fn leap(year: i32) -> bool {
    (year % 4 == 0 && year % 100 != 0) || year % 400 == 0
}

fn month_length(year: i32, month: u32) -> u32 {
    match month {
        2 if leap(year) => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    }
}

impl Now {
    /// A date and time of the Gregorian calendar, year 1 to 9999 as Python's `datetime` has
    /// them.
    ///
    /// # Errors
    /// [`Error::Template`] when a field is out of range (month 13, 30 February, hour 24).
    pub fn new(
        year: i32,
        month: u32,
        day: u32,
        (hour, minute, second): (u32, u32, u32),
    ) -> Result<Self, Error> {
        let date = (1..=9999).contains(&year)
            && (1..=12).contains(&month)
            && (1..=month_length(year, month)).contains(&day);
        if !date || hour > 23 || minute > 59 || second > 59 {
            return Err(Error::Template(format!(
                "now: {year}-{month}-{day} {hour}:{minute}:{second} is not a date and time"
            )));
        }
        Ok(Self {
            year,
            month,
            day,
            hour,
            minute,
            second,
        })
    }

    /// The day of the year, 1 to 366.
    fn year_day(self) -> u32 {
        let after_february = u32::from(self.month > 2 && leap(self.year));
        BEFORE_MONTH[self.month as usize - 1] + self.day + after_february
    }

    /// The day of the week, Monday 0 to Sunday 6.
    fn week_day(self) -> u32 {
        let year = self.year - 1;
        // 1 January of year 1 was a Monday.
        let days = year * 365 + year / 4 - year / 100 + year / 400;
        (days.unsigned_abs() + self.year_day() - 1) % 7
    }

    fn hour12(self) -> u32 {
        match self.hour % 12 {
            0 => 12,
            hour => hour,
        }
    }

    /// One directive's text; `None` for a directive this does not print.
    fn directive(self, letter: char) -> Option<String> {
        let month = MONTHS[self.month as usize - 1];
        let day = DAYS[self.week_day() as usize];
        Some(match letter {
            'Y' => self.year.to_string(),
            'y' => format!("{:02}", self.year % 100),
            'm' => format!("{:02}", self.month),
            'd' => format!("{:02}", self.day),
            'e' => format!("{:2}", self.day),
            'H' => format!("{:02}", self.hour),
            'I' => format!("{:02}", self.hour12()),
            'M' => format!("{:02}", self.minute),
            'S' => format!("{:02}", self.second),
            'j' => format!("{:03}", self.year_day()),
            'p' => if self.hour < 12 { "AM" } else { "PM" }.to_owned(),
            'B' => month.to_owned(),
            'b' | 'h' => month[..3].to_owned(),
            'A' => day.to_owned(),
            'a' => day[..3].to_owned(),
            'u' => (self.week_day() + 1).to_string(),
            'w' => ((self.week_day() + 1) % 7).to_string(),
            'f' => "000000".to_owned(),
            'Z' | 'z' => String::new(),
            '%' => "%".to_owned(),
            'n' => "\n".to_owned(),
            't' => "\t".to_owned(),
            _ => return None,
        })
    }

    /// A directive that stands for several (`%F` is `%Y-%m-%d`).
    fn composite(letter: char) -> Option<&'static str> {
        Some(match letter {
            'F' => "%Y-%m-%d",
            'T' | 'X' => "%H:%M:%S",
            'R' => "%H:%M",
            'D' | 'x' => "%m/%d/%y",
            'c' => "%a %b %e %H:%M:%S %Y",
            'r' => "%I:%M:%S %p",
            _ => return None,
        })
    }

    /// `strftime(format)` in the "C" locale. `%-d` (glibc: no padding) is understood for the
    /// numeric directives.
    ///
    /// # Errors
    /// [`Error::Template`] for a directive this does not print (week numbers, `%s`, a locale's
    /// alternatives): a guess here would be a wrong count.
    pub fn strftime(self, format: &str) -> Result<String, Error> {
        let mut out = String::new();
        let mut chars = format.chars();
        while let Some(c) = chars.next() {
            if c != '%' {
                out.push(c);
                continue;
            }
            let mut letter = chars.next();
            let bare = letter == Some('-');
            if bare {
                letter = chars.next();
            }
            let unknown = || {
                let shown = letter.map(String::from).unwrap_or_default();
                Error::Template(format!(
                    "strftime_now: %{shown} in {format:?} is not supported"
                ))
            };
            let letter = letter.ok_or_else(unknown)?;
            if let Some(expansion) = Self::composite(letter) {
                out.push_str(&self.strftime(expansion)?);
                continue;
            }
            let text = self.directive(letter).ok_or_else(unknown)?;
            let text = if bare {
                text.trim_start_matches(['0', ' '])
            } else {
                &text
            };
            if bare && text.is_empty() {
                out.push('0');
            }
            let _ = write!(out, "{text}");
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn prints_what_c_strftime_prints() {
        let now = Now::new(2024, 7, 26, (9, 5, 0)).unwrap();
        assert_eq!(now.strftime("%d %b %Y").unwrap(), "26 Jul 2024");
        assert_eq!(
            now.strftime("%A, %B %-d, %Y %I:%M %p").unwrap(),
            "Friday, July 26, 2024 09:05 AM"
        );
        assert_eq!(
            now.strftime("%F %T %j %a %y %%").unwrap(),
            "2024-07-26 09:05:00 208 Fri 24 %"
        );
        assert_eq!(now.strftime("%-H:%-M:%-S %e").unwrap(), "9:5:0 26");
        let leap_day = Now::new(2000, 2, 29, (0, 0, 0)).unwrap();
        assert_eq!(leap_day.strftime("%A %j %u %w").unwrap(), "Tuesday 060 2 2");
        assert_eq!(
            Now::new(1, 1, 1, (0, 0, 0))
                .unwrap()
                .strftime("%A")
                .unwrap(),
            "Monday"
        );
        assert_eq!(
            Now::new(2026, 11, 8, (0, 0, 0))
                .unwrap()
                .strftime("%a %w %u")
                .unwrap(),
            "Sun 0 7"
        );
    }

    #[test]
    fn refuses_what_it_does_not_print_and_what_is_no_date() {
        let now = Now::new(2024, 7, 26, (9, 5, 0)).unwrap();
        assert!(now.strftime("%U").is_err());
        assert!(now.strftime("100%").is_err());
        assert!(Now::new(2023, 2, 29, (0, 0, 0)).is_err());
        assert!(Now::new(2024, 1, 1, (24, 0, 0)).is_err());
    }
}
