# Wardline — Full Scenario

A full scenario from start to finish, told from the people's side, with what the system does behind the scenes.

## 2:10 AM: the attack

Mercy General's unit 4 is quiet. Dana, the charge nurse, is doing rounds. Six nurses are covering 32 patients.

IT spots ransomware spreading and **shuts down every hospital system.** The records system, the pharmacy system, and the hospital Wi-Fi all go dark. Screens freeze. The paper backup binder was printed 6 hours ago.

> **Behind the scenes:** the hospital's health check stops responding. EventBridge fires `OutageDetected`. The patient data is already safe in the isolated AWS backup, last synced at 2:05.

## 2:11 AM: everyone gets the message

Every nurse's personal phone buzzes, on cellular, not hospital Wi-Fi:

> "WARDLINE: Hospital systems down. Your patients' info is available here: [link]. You have 2 critical meds due within 30 min. Tap to confirm."

> **Behind the scenes:** the **alert agent** looked at each nurse's patient list, texted the ones with critical patients first, and wrote a personal message for each.

## 2:12 AM: Lee opens her link

Nurse Lee taps the link and enters her PIN. She doesn't have to ask anything. The page opens with:

> "Records as of 2:05 AM. Your top 3:
>
> 1. Room 304: insulin due 2:20
> 2. Room 306: penicillin allergy, don't give amoxicillin
> 3. Room 310: last BP 88/50, recheck"

She asks, "What else is due in the next hour?" and gets her full list. She **only ever sees her own 5 patients.**

> **Behind the scenes:** the **nurse agent** received only Lee's patients from DynamoDB. It literally has no access to anyone else's.

## 2:16 AM: Kim doesn't respond

Nurse Kim is in a patient's room, hands full, and hasn't seen her phone. Wardline resends at 2:13. Still nothing.

At 2:16, Dana's phone buzzes:

> "Kim hasn't confirmed. Her patients in 308 and 312 have meds due before 2:45. Please check on her."

Dana finds Kim, who opens her link.

> **Behind the scenes:** the **alert agent** tracked acknowledgments, resent, then escalated. No one falls through the cracks.

## 2:17 AM: no insulin in the cabinet

Lee goes to the unit cabinet. No insulin. She types:

> "304's insulin isn't in the cabinet."

The reply:

> "Requested. Insulin is coming from Unit 6. Marcus is bringing it, ETA 2:20."

Meanwhile Marcus, the pharmacy runner, gets a text:

> "URGENT: insulin, Unit 6 cabinet → Room 304 by 2:20."

> **Behind the scenes:** the **nurse agent** sent `MedRequested` to the routing agent. It checked stock (central pharmacy is far, but Unit 6 is [closer]...)

## 2:20 AM: something the AI can't solve

*(Not captured in the source screenshots.)*

## 6:40 AM: systems come back

IT restores the records system. Dana gets a report:

> "Downtime 2:10–6:40. 47 meds given, 12 vitals recorded, 3 notes, all organized by patient for entry.
>
> Needs review: Room 310 heparin, delayed 25 min (pharmacist decision at 2:34)."

> **Behind the scenes:** the **recovery agent** read the full downtime log and flagged what humans need to check.

## How the night ended

- Every nurse was reached within 6 minutes.
- Every critical dose was delivered or escalated to a human.
- One double dose was prevented.
- Nothing from the downtime was lost.

**Without Wardline:** paper binders 6 hours old, nurses phoning pharmacy, runners guessing priorities, and no way to know who already gave what.
