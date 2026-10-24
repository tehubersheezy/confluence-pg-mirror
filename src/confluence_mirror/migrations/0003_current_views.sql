-- 0003: convenience views over the current (latest mirrored, non-deleted) version.

CREATE VIEW extracted.current_page_content AS
SELECT p.page_id, p.space_id, p.title, c.version_number,
       c.body_storage, c.body_view, c.body_export_view
FROM source.pages p
JOIN source.page_content c
  ON c.page_id = p.page_id AND c.version_number = p.current_version
WHERE NOT p.is_deleted;

CREATE VIEW extracted.current_page_macros AS
SELECT m.* FROM extracted.page_macros m
JOIN source.pages p ON p.page_id = m.page_id AND p.current_version = m.version_number
WHERE NOT p.is_deleted;

CREATE VIEW extracted.current_page_tables AS
SELECT t.* FROM extracted.page_tables t
JOIN source.pages p ON p.page_id = t.page_id AND p.current_version = t.version_number
WHERE NOT p.is_deleted;

CREATE VIEW extracted.current_page_links AS
SELECT l.*, (tp.page_id IS NOT NULL AND NOT tp.is_deleted) AS target_in_mirror
FROM extracted.page_links l
JOIN source.pages p ON p.page_id = l.page_id AND p.current_version = l.version_number
LEFT JOIN source.pages tp ON tp.page_id = l.target_page_id
WHERE NOT p.is_deleted;
