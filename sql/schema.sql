/* =========================================================================
   Reliance Retail SKU Label OCR — database schema
   Target: Microsoft SQL Server 2022 (Express is sufficient for the POC)

   Idempotent. Safe to run repeatedly.

   Apply with:
       sqlcmd -S localhost\SQLEXPRESS -E -i sql\schema.sql

   Design notes:
   - Image binaries are NOT stored here. Only the filesystem path is kept
     (CLAUDE.md section 20).
   - Raw OCR tokens ARE stored during R&D. Field-level accuracy is the whole
     point of the project, and a failed scan cannot be diagnosed without the
     tokens that produced it (section 16).
   - There is one database. No separate store for OCR metadata (section 15).
   ========================================================================= */

IF DB_ID(N'RetailOcr') IS NULL
BEGIN
    CREATE DATABASE RetailOcr;
END
GO

USE RetailOcr;
GO

/* -------------------------------------------------------------------------
   SkuScan — one row per captured image.

   ScanCode is the human-facing identifier (SCAN-000001). It is what the
   operator sees, what appears in logs, and what leads the QR payload.
   Id stays a GUID so scans can be created without a round trip.
   ------------------------------------------------------------------------- */
IF OBJECT_ID(N'dbo.SkuScan', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.SkuScan
    (
        Id                  UNIQUEIDENTIFIER    NOT NULL
            CONSTRAINT PK_SkuScan PRIMARY KEY DEFAULT NEWSEQUENTIALID(),
        ScanCode            NVARCHAR(32)        NOT NULL,
        DeviceId            NVARCHAR(128)       NULL,
        DeviceModel         NVARCHAR(128)       NULL,
        ImagePath           NVARCHAR(512)       NULL,
        ProcessingStatus    NVARCHAR(32)        NOT NULL,
        OverallConfidence   DECIMAL(5, 4)       NULL,
        OcrVariantUsed      NVARCHAR(32)        NULL,
        FailureReason       NVARCHAR(1024)      NULL,
        ProcessingMs        INT                 NULL,
        CreatedAt           DATETIME2(3)        NOT NULL CONSTRAINT DF_SkuScan_CreatedAt DEFAULT SYSUTCDATETIME(),
        ConfirmedAt         DATETIME2(3)        NULL,
        PrintedAt           DATETIME2(3)        NULL,

        /* Statuses the API may return. Kept as a CHECK rather than a lookup
           table so the allowed set is visible in one place and cheap to
           extend. NO_TEXT_DETECTED is a valid outcome, not an error
           (CLAUDE.md section 22). */
        CONSTRAINT CK_SkuScan_ProcessingStatus CHECK
        (
            ProcessingStatus IN
            (
                N'PENDING',
                N'PROCESSING',
                N'COMPLETED',
                N'NO_TEXT_DETECTED',
                N'FAILED',
                N'CONFIRMED',
                N'PRINTED'
            )
        ),
        CONSTRAINT UQ_SkuScan_ScanCode UNIQUE (ScanCode)
    );

    CREATE INDEX IX_SkuScan_CreatedAt ON dbo.SkuScan (CreatedAt DESC);
    CREATE INDEX IX_SkuScan_ProcessingStatus ON dbo.SkuScan (ProcessingStatus);
END
GO

/* -------------------------------------------------------------------------
   SkuScanField — one row per extracted field per scan.

   Three value columns, deliberately:
     RawValue        what OCR produced, untouched
     NormalizedValue what the server derived from it (section 10)
     ConfirmedValue  what the operator accepted or typed (section 4)

   Keeping all three is what makes the manual-correction rate of section 24
   measurable. Collapsing them would destroy the metric.
   ------------------------------------------------------------------------- */
IF OBJECT_ID(N'dbo.SkuScanField', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.SkuScanField
    (
        Id                  BIGINT              IDENTITY(1, 1)
            CONSTRAINT PK_SkuScanField PRIMARY KEY,
        ScanId              UNIQUEIDENTIFIER    NOT NULL,
        FieldName           NVARCHAR(64)        NOT NULL,
        RawValue            NVARCHAR(256)       NULL,
        NormalizedValue     NVARCHAR(256)       NULL,
        ConfirmedValue      NVARCHAR(256)       NULL,
        Confidence          DECIMAL(5, 4)       NULL,
        Source              NVARCHAR(32)        NULL,
        -- Which engines produced a value for this field: "OCR", "VLM", or both.
        Engines             NVARCHAR(32)        NULL,
        -- What the OTHER engine read, when the two disagreed.
        --
        -- Kept because the operator's confirmed value settles which engine was
        -- right, and that is the only evidence there is for a question the
        -- merge currently answers by convention: it keeps the primary, not
        -- because PP-OCRv5 is more often correct, but so the result stays
        -- predictable. On the first real contested scan the VLM was right.
        -- Enough of these and the default becomes a measured choice rather
        -- than an assumption, possibly per field.
        ConflictValue       NVARCHAR(256)       NULL,
        WasEdited           BIT                 NOT NULL CONSTRAINT DF_SkuScanField_WasEdited DEFAULT 0,
        ValidationNote      NVARCHAR(512)       NULL,
        CreatedAt           DATETIME2(3)        NOT NULL CONSTRAINT DF_SkuScanField_CreatedAt DEFAULT SYSUTCDATETIME(),

        CONSTRAINT FK_SkuScanField_SkuScan FOREIGN KEY (ScanId)
            REFERENCES dbo.SkuScan (Id) ON DELETE CASCADE,
        CONSTRAINT UQ_SkuScanField_ScanField UNIQUE (ScanId, FieldName)
    );

    CREATE INDEX IX_SkuScanField_ScanId ON dbo.SkuScanField (ScanId);
END
GO

/* -------------------------------------------------------------------------
   SkuScanOcr — raw OCR tokens with spatial data.

   Bounding boxes are stored as X/Y/Width/Height rather than a serialized
   blob so failures can be inspected with plain SQL. Spatial association
   (section 9) is the most likely place for extraction to go wrong, and it
   cannot be debugged from concatenated text.

   VariantName records which preprocessing variant produced the token, so
   cross-variant agreement (section 12) can be reconstructed after the fact.
   ------------------------------------------------------------------------- */
IF OBJECT_ID(N'dbo.SkuScanOcr', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.SkuScanOcr
    (
        Id                  BIGINT              IDENTITY(1, 1)
            CONSTRAINT PK_SkuScanOcr PRIMARY KEY,
        ScanId              UNIQUEIDENTIFIER    NOT NULL,
        VariantName         NVARCHAR(32)        NULL,
        TokenIndex          INT                 NOT NULL,
        Text                NVARCHAR(512)       NULL,
        X                   INT                 NULL,
        Y                   INT                 NULL,
        Width               INT                 NULL,
        Height              INT                 NULL,
        Confidence          DECIMAL(5, 4)       NULL,
        CreatedAt           DATETIME2(3)        NOT NULL CONSTRAINT DF_SkuScanOcr_CreatedAt DEFAULT SYSUTCDATETIME(),

        CONSTRAINT FK_SkuScanOcr_SkuScan FOREIGN KEY (ScanId)
            REFERENCES dbo.SkuScan (Id) ON DELETE CASCADE
    );

    CREATE INDEX IX_SkuScanOcr_ScanId ON dbo.SkuScanOcr (ScanId);
END
GO

/* -------------------------------------------------------------------------
   SkuMaster — optional product master data (section 15).

   Created empty. Reliance Retail master data availability is still open (PLAN.md Q7).
   Useful later for product context and barcode/GTIN matching.
   ------------------------------------------------------------------------- */
IF OBJECT_ID(N'dbo.SkuMaster', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.SkuMaster
    (
        Id                  BIGINT              IDENTITY(1, 1)
            CONSTRAINT PK_SkuMaster PRIMARY KEY,
        Gtin                NVARCHAR(32)        NULL,
        Sku                 NVARCHAR(64)        NULL,
        ProductName         NVARCHAR(256)       NULL,
        Manufacturer        NVARCHAR(256)       NULL,
        CreatedAt           DATETIME2(3)        NOT NULL CONSTRAINT DF_SkuMaster_CreatedAt DEFAULT SYSUTCDATETIME()
    );

    CREATE INDEX IX_SkuMaster_Gtin ON dbo.SkuMaster (Gtin);
    CREATE INDEX IX_SkuMaster_Sku ON dbo.SkuMaster (Sku);
END
GO

/* -------------------------------------------------------------------------
   ScanCode sequence.

   A sequence rather than a computed column: scan codes must be stable and
   gap-tolerant, and IDENTITY on a GUID-keyed table would be a second
   surrogate key. Formatted as SCAN-000001 by the application.
   ------------------------------------------------------------------------- */
IF NOT EXISTS (SELECT 1 FROM sys.sequences WHERE name = N'SeqScanCode')
BEGIN
    CREATE SEQUENCE dbo.SeqScanCode
        AS BIGINT
        START WITH 1
        INCREMENT BY 1
        NO CYCLE;
END
GO
