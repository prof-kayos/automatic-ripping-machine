"""Pure-logic ripping policy for the ARM fork (stdlib only, no hardware, no network).

Modules:
  makemkv_scan   parse ``makemkvcon -r info`` robot output
  title_select   choose the main movie title (extended cut preferred)
  selection      build the MakeMKV track-selection string (English, + original audio for foreign films)
  quality        score a disc title / existing file and decide replace / skip / ask
  library_match  fuzzy-match a disc's movie to a ``Title (Year)`` library folder
  decision       combine everything into one auditable Plan
"""
