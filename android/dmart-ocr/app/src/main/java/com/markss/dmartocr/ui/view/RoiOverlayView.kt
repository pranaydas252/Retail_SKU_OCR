package com.markss.dmartocr.ui.view

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Path
import android.graphics.PorterDuff
import android.graphics.PorterDuffXfermode
import android.graphics.RectF
import android.util.AttributeSet
import android.view.View
import androidx.core.content.ContextCompat
import com.markss.dmartocr.R

/**
 * The region-of-interest window drawn over the camera preview.
 *
 * This view is both the aiming guide and the definition of the crop. The
 * operator never crops anything by hand (CLAUDE.md section 4) — they align the
 * label to this window, and the app crops the capture to the same rectangle.
 * Keeping both in one place is what stops the guide and the crop from drifting
 * apart.
 *
 * The rectangle is expressed as fractions of the view, so it survives any
 * preview resolution. [roiFraction] is what [com.markss.dmartocr.ui.ScanActivity]
 * applies to the captured bitmap.
 */
class RoiOverlayView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
    defStyleAttr: Int = 0,
) : View(context, attrs, defStyleAttr) {

    /**
     * ROI geometry, as a fraction of the view.
     *
     * Wide and short, because SKU label blocks are wider than they are tall and
     * a squarer window invites the operator to include surrounding packaging —
     * which costs OCR time directly, since latency scales with the number of
     * detected text regions (PLAN.md R8).
     */
    private val widthFraction = 0.88f
    private val heightFraction = 0.30f
    private val verticalBias = 0.42f

    private val cornerRadius = 20f * resources.displayMetrics.density
    private val strokeWidth = 3f * resources.displayMetrics.density
    private val cornerArm = 26f * resources.displayMetrics.density

    private val scrimPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = ContextCompat.getColor(context, R.color.scrim)
    }

    private val clearPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        xfermode = PorterDuffXfermode(PorterDuff.Mode.CLEAR)
    }

    private val edgePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        color = Color.WHITE
        alpha = 64
        strokeWidth = this@RoiOverlayView.strokeWidth * 0.5f
    }

    private val cornerPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        color = ContextCompat.getColor(context, R.color.roi_stroke)
        strokeWidth = this@RoiOverlayView.strokeWidth
        strokeCap = Paint.Cap.ROUND
        strokeJoin = Paint.Join.ROUND
    }

    private val roiRect = RectF()
    private val cornerPath = Path()

    init {
        // The CLEAR xfermode needs its own layer to punch through the scrim.
        setLayerType(LAYER_TYPE_HARDWARE, null)
    }

    /** ROI in view pixels. Valid after layout. */
    fun roiRect(): RectF = RectF(roiRect)

    /**
     * ROI as fractions of the view: left, top, right, bottom in 0..1.
     *
     * These map directly onto the captured bitmap. Because preview and capture
     * are bound to a shared `ViewPort`, both cover the same field of view, so
     * the same fractions describe the same physical region in each (PLAN.md
     * R14).
     */
    fun roiFraction(): RectF {
        if (width == 0 || height == 0) return RectF(0f, 0f, 1f, 1f)
        return RectF(
            roiRect.left / width,
            roiRect.top / height,
            roiRect.right / width,
            roiRect.bottom / height,
        )
    }

    override fun onSizeChanged(w: Int, h: Int, oldw: Int, oldh: Int) {
        super.onSizeChanged(w, h, oldw, oldh)
        val roiWidth = w * widthFraction
        val roiHeight = h * heightFraction
        val left = (w - roiWidth) / 2f
        val top = (h - roiHeight) * verticalBias
        roiRect.set(left, top, left + roiWidth, top + roiHeight)
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)

        canvas.drawRect(0f, 0f, width.toFloat(), height.toFloat(), scrimPaint)
        canvas.drawRoundRect(roiRect, cornerRadius, cornerRadius, clearPaint)
        canvas.drawRoundRect(roiRect, cornerRadius, cornerRadius, edgePaint)

        drawCorners(canvas)
    }

    /**
     * Corner brackets rather than a full border. They mark the target without
     * putting a continuous line next to the label text, which would compete
     * with the printing the operator is trying to line up.
     */
    private fun drawCorners(canvas: Canvas) {
        cornerPath.reset()
        val r = cornerRadius

        // Top-left
        cornerPath.moveTo(roiRect.left, roiRect.top + cornerArm)
        cornerPath.lineTo(roiRect.left, roiRect.top + r)
        cornerPath.quadTo(roiRect.left, roiRect.top, roiRect.left + r, roiRect.top)
        cornerPath.lineTo(roiRect.left + cornerArm, roiRect.top)

        // Top-right
        cornerPath.moveTo(roiRect.right - cornerArm, roiRect.top)
        cornerPath.lineTo(roiRect.right - r, roiRect.top)
        cornerPath.quadTo(roiRect.right, roiRect.top, roiRect.right, roiRect.top + r)
        cornerPath.lineTo(roiRect.right, roiRect.top + cornerArm)

        // Bottom-right
        cornerPath.moveTo(roiRect.right, roiRect.bottom - cornerArm)
        cornerPath.lineTo(roiRect.right, roiRect.bottom - r)
        cornerPath.quadTo(roiRect.right, roiRect.bottom, roiRect.right - r, roiRect.bottom)
        cornerPath.lineTo(roiRect.right - cornerArm, roiRect.bottom)

        // Bottom-left
        cornerPath.moveTo(roiRect.left + cornerArm, roiRect.bottom)
        cornerPath.lineTo(roiRect.left + r, roiRect.bottom)
        cornerPath.quadTo(roiRect.left, roiRect.bottom, roiRect.left, roiRect.bottom - r)
        cornerPath.lineTo(roiRect.left, roiRect.bottom - cornerArm)

        canvas.drawPath(cornerPath, cornerPaint)
    }
}
