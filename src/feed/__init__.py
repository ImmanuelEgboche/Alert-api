"""Record, replay and measure the exchange feed.

- recording: on-disk format for the raw feed
- record:    CLI that captures a live exchange feed to a recording
- replay:    plays a recording back through the same pipeline as live
- completeness: trade-ID gap / duplicate detection
"""
