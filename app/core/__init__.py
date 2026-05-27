"""Cross-cutting infrastructure: config, DB, logging, errors, time helpers.

Здесь лежат модули, от которых зависят все домены, но которые сами не
знают ни о каком домене. Если новая утилита нужна и ``auth``, и
``notebooks`` — её место именно тут.
"""
