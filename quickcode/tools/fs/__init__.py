"""The filesystem underneath the file tools.

``textfile`` is how read, edit and write turn bytes into text and back without
losing the encoding, the byte-order mark or the line endings a file had.
``patterns`` and ``walk`` are how glob and grep's pure-Python search decide
which files a pattern names and which ones a search should never visit.
"""
