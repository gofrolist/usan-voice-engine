// Curated US IANA zones for the Contacts editor. The backend accepts ANY valid
// IANA zone (see schemas/_validators.validate_iana_timezone), so an existing
// out-of-list value is preserved in the UI rather than forced into this set.
export interface TimezoneOption {
  value: string;
  label: string;
}

export const US_TIMEZONES: readonly TimezoneOption[] = [
  { value: "America/New_York", label: "Eastern (America/New_York)" },
  { value: "America/Chicago", label: "Central (America/Chicago)" },
  { value: "America/Denver", label: "Mountain (America/Denver)" },
  { value: "America/Phoenix", label: "Arizona — no DST (America/Phoenix)" },
  { value: "America/Los_Angeles", label: "Pacific (America/Los_Angeles)" },
  { value: "America/Anchorage", label: "Alaska (America/Anchorage)" },
  { value: "Pacific/Honolulu", label: "Hawaii (Pacific/Honolulu)" },
];
