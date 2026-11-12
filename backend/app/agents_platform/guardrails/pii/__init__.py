"""
Personal data typed into the chat.

    validators  checksums (Luhn, Verhoeff) and format checks that separate real identifiers from look-alike numbers
    detectors   what to look for and what to do about each class (mask, or refuse the message)
    masker      `<EMAIL_1>` tokens in, original values back out for the classes that may be shown
    vault       where the original values wait between masking and restoring (encrypted when shared)

The model, the checkpoint and the logs only ever see masked text.
"""
