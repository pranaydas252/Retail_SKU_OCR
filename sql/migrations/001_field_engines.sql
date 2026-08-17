-- Record which engines read a field, and what the losing one said.
--
-- Run against an existing RetailOcr database. schema.sql already creates these
-- columns, so a database built fresh from it does not need this.
--
-- Why the losing value is worth a column: when PP-OCRv5 and the
-- vision-language model disagree, the merge keeps the primary. That is a
-- decision made for predictability, not because PP-OCRv5 is more often right -
-- and on the first contested scan captured on real hardware, the VLM was the
-- correct one. The operator's ConfirmedValue settles each contest, so pairing
-- it with ConflictValue is the only evidence that can turn the merge rule from
-- an assumption into a measurement, possibly a different one per field.

USE RetailOcr;
GO

IF COL_LENGTH('dbo.SkuScanField', 'Engines') IS NULL
BEGIN
    ALTER TABLE dbo.SkuScanField ADD Engines NVARCHAR(32) NULL;
    PRINT 'Added SkuScanField.Engines';
END
ELSE
    PRINT 'SkuScanField.Engines already present';
GO

IF COL_LENGTH('dbo.SkuScanField', 'ConflictValue') IS NULL
BEGIN
    ALTER TABLE dbo.SkuScanField ADD ConflictValue NVARCHAR(256) NULL;
    PRINT 'Added SkuScanField.ConflictValue';
END
ELSE
    PRINT 'SkuScanField.ConflictValue already present';
GO

-- Who wins a contest, once there are enough of them to say.
--
-- A row per contested field: what each engine read, and which one the operator
-- kept. "primary" means PP-OCRv5's reading survived confirmation, "secondary"
-- means the operator replaced it with the VLM's.
CREATE OR ALTER VIEW dbo.ContestedFields
AS
SELECT
    s.ScanCode,
    s.CreatedAt,
    f.FieldName,
    f.NormalizedValue          AS PrimaryReading,
    f.ConflictValue            AS SecondaryReading,
    f.ConfirmedValue,
    CASE
        WHEN f.ConfirmedValue IS NULL                   THEN 'unconfirmed'
        WHEN f.ConfirmedValue = f.NormalizedValue       THEN 'primary'
        WHEN f.ConfirmedValue = f.ConflictValue         THEN 'secondary'
        ELSE 'neither'
    END                        AS OperatorKept
FROM dbo.SkuScanField AS f
JOIN dbo.SkuScan      AS s ON s.Id = f.ScanId
WHERE f.ConflictValue IS NOT NULL;
GO

PRINT 'Migration 001 complete. Query dbo.ContestedFields to see who wins.';
GO
