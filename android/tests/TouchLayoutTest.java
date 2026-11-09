// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 S2Arm contributors
package org.s2arm.android;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Arrays;

/** F01 contract tests; expected geometry is calculated from the documented units. */
public final class TouchLayoutTest {
    private static int checks;
    private static String reference;

    private static void check(boolean condition, String message) {
        checks++;
        if (!condition) throw new AssertionError(message);
    }

    private static void near(double actual, double expected, String message) {
        check(Math.abs(actual - expected) < 1e-8, message + ": " + actual + " != " + expected);
    }

    private static TouchLayout read(String text) {
        return TouchLayout.read(text.getBytes(StandardCharsets.UTF_8));
    }

    private static void rejects(String text, String message) {
        rejectsBytes(text.getBytes(StandardCharsets.UTF_8), message);
    }

    private static void rejectsBytes(byte[] bytes, String message) {
        checks++;
        try { TouchLayout.read(bytes); }
        catch (IllegalArgumentException expected) { return; }
        throw new AssertionError("accepted " + message);
    }

    private static void invalidEdit(Runnable edit, String message) {
        checks++;
        try { edit.run(); }
        catch (IllegalArgumentException expected) { return; }
        throw new AssertionError("accepted edit " + message);
    }

    public static void main(String[] args) throws Exception {
        reference = Files.readString(Path.of(args[0]), StandardCharsets.UTF_8);
        TouchLayout original = read(reference);
        check(original.buttons.size() == 14 && original.sticks.size() == 2, "existing iOS reference");
        check(original.identifier.equals("classic-reference"), "reference identity");
        String[] ids = {"A", "B", "X", "Y", "L", "R", "ZL", "ZR", "PLUS", "MINUS",
                "LEFT", "UP", "RIGHT", "DOWN"};
        for (String id : ids) check(original.button(id).id.equals(id), "required " + id);
        check(original.stick("left").id.equals("left") && original.stick("right").id.equals("right"), "sticks");

        // Fourteen controls are required, LS and RS may be added but not duplicated.
        int arrayEnd = reference.indexOf("\"sticks\"");
        int close = reference.lastIndexOf(']', arrayEnd);
        String extra = ",{\"id\":\"LS\",\"x\":0.2,\"y\":0.2,\"width\":0.1,\"height\":0.1,\"cornerRadius\":0.05}";
        String withLs = reference.substring(0, close) + extra + reference.substring(close);
        check(read(withLs).buttons.size() == 15, "optional LS");
        String withBoth = reference.substring(0, close) + extra + extra.replace("LS", "RS") + reference.substring(close);
        check(read(withBoth).buttons.size() == 16, "optional LS and RS");
        rejects(reference.substring(0, close) + extra + extra + reference.substring(close), "duplicate button");

        rejects(reference.replaceFirst("\"A\"", "\"B\""), "duplicate/missing required id");
        rejects(reference.replaceFirst("\"A\"", "\"LS\""), "missing required A");
        rejects(reference.replaceFirst("\"A\"", "\"HOME\""), "unknown button");
        rejects(reference.replaceFirst("\"left\"", "\"right\""), "duplicate stick");
        rejects(reference.replaceFirst("\"left\"", "\"other\""), "unknown stick");
        rejects(reference.replaceFirst("\\{", "{\"extra\":0,"), "unknown top field");
        rejects(reference.replaceFirst("\"A\",", "\"A\",\"extra\":0,"), "unknown button field");
        rejects(reference.replaceFirst("\"left\",", "\"left\",\"extra\":0,"), "unknown stick field");
        rejects(reference.replaceFirst("\"activation\": \\{", "\"activation\": {\"extra\":0,"), "unknown activation field");
        rejects(reference.replaceFirst("\"schemaVersion\": 1", "\"schemaVersion\": 2"), "version 2");
        rejects(reference.replaceFirst("\"schemaVersion\": 1", "\"schemaVersion\": true"), "boolean version");
        rejects(reference.replaceFirst("\"schemaVersion\": 1", "\"schemaVersion\": 1.5"), "fractional version");
        for (String bad : new String[] {"01", "1.", "1e", "-01", "1e9999"})
            rejects(reference.replaceFirst("\"schemaVersion\": 1", "\"schemaVersion\": " + bad), "number " + bad);
        // Integer-valued JSON numbers are integer schema values (1.0 and 1e0 are valid).
        read(reference.replaceFirst("\"schemaVersion\": 1", "\"schemaVersion\": 1.0"));
        read(reference.replaceFirst("\"schemaVersion\": 1", "\"schemaVersion\": 1e0"));
        rejects(reference + "x", "trailing bytes");
        rejects(reference.replaceFirst("\"id\":", "\"id\":\"duplicate\",\"id\":"), "duplicate JSON key");
        rejectsBytes(new byte[] {(byte) 0xc3, (byte) 0x28}, "invalid UTF-8");
        byte[] maximum = new byte[65536];
        Arrays.fill(maximum, (byte) ' ');
        byte[] valid = reference.getBytes(StandardCharsets.UTF_8);
        System.arraycopy(valid, 0, maximum, 0, valid.length);
        check(TouchLayout.read(maximum).buttons.size() == 14, "64 KiB inclusive");
        rejectsBytes(Arrays.copyOf(maximum, maximum.length + 1), "over 64 KiB");
        rejects(reference.replace("classic-reference", "../escape"), "path identity");
        rejects(reference.replace("classic-reference", "Upper"), "uppercase identity");
        rejects(reference.replaceFirst("\"name\": \"[^\"]+\"", "\"name\": \"\""), "empty name");
        rejects(reference.replaceFirst("\"name\": \"[^\"]+\"", "\"name\": \"bad" + (char) 127 + "\""), "control name");

        TouchLayout edited = original.withButton("A", 0.99, 0.01, 0.2, 0.1, 0.05);
        check(edited != original, "edit returns independent model");
        near(edited.button("A").x, 0.99, "edited centre");
        check(original.button("A").x != edited.button("A").x, "original remains unchanged");
        near(edited.button("B").x, original.button("B").x, "other button preserved");
        TouchLayout.Rect frame = edited.frameForButton("A", 40, 100, 1000, 500);
        // Unit = 500, width = 100, height = 50; centre = (1030, 105), clamp to safe rect.
        near(frame.x, 940, "right clamp"); near(frame.y, 100, "top clamp");
        near(frame.width, 100, "short-side width"); near(frame.height, 50, "short-side height");
        TouchLayout.Rect portrait = edited.frameForButton("A", 20, 80, 300, 700);
        near(portrait.width, 60, "portrait short side"); near(portrait.x, 260, "portrait safe right");
        near(portrait.y, 80, "portrait safe top");
        invalidEdit(() -> edited.frameForButton("A", 0, 0, 0, 100), "zero viewport");
        invalidEdit(() -> edited.frameForButton("A", Double.NaN, 0, 100, 100), "NaN viewport");
        TouchLayout stickLayout = original.withStick("left", 0, 0, 0.5, 1, 0.1);
        TouchLayout.Rect landscapeStick = stickLayout.frameForStick("left", 40, 100, 1000, 500);
        // Activation centre (0.25, 0.5) -> (290, 350), radius 0.1 * 500 = 50.
        near(landscapeStick.x, 240, "stick landscape x");
        near(landscapeStick.y, 300, "stick landscape y");
        near(landscapeStick.width, 100, "stick landscape diameter");
        near(landscapeStick.height, 100, "stick landscape circle");
        TouchLayout portraitLayout = original.withStick("right", 0.5, 0, 0.5, 1, 0.12);
        TouchLayout.Rect portraitStick = portraitLayout.frameForStick("right", 20, 80, 300, 700);
        // Activation centre (0.75, 0.5) -> (245, 430), radius 0.12 * 300 = 36.
        near(portraitStick.x, 209, "stick portrait x");
        near(portraitStick.y, 394, "stick portrait y");
        near(portraitStick.width, 72, "stick portrait diameter");
        near(portraitStick.height, 72, "stick portrait circle");
        TouchLayout edgeLayout = original.withStick("left", 0, 0, 0.1, 0.1, 0.2);
        TouchLayout.Rect edgeStick = edgeLayout.frameForStick("left", 40, 100, 1000, 500);
        near(edgeStick.x, 40, "stick safe left clamp");
        near(edgeStick.y, 100, "stick safe top clamp");
        near(edgeStick.width, 200, "stick clamped size unchanged");
        TouchLayout bottomLayout = original.withStick("right", 0.9, 0.9, 0.1, 0.1, 0.3);
        TouchLayout.Rect bottomStick = bottomLayout.frameForStick("right", 40, 100, 1000, 500);
        near(bottomStick.x, 740, "stick safe right clamp");
        near(bottomStick.y, 300, "stick safe bottom clamp");
        invalidEdit(() -> original.frameForStick("left", 0, 0, 0, 100), "stick zero viewport");
        invalidEdit(() -> original.frameForStick("left", 0, Double.NaN, 100, 100), "stick NaN viewport");
        invalidEdit(() -> original.frameForStick("missing", 0, 0, 100, 100), "unknown projected stick");
        invalidEdit(() -> original.withButton("A", -0.01, 0, 0.1, 0.1, 0), "negative x");
        invalidEdit(() -> original.withButton("A", 0, 1.01, 0.1, 0.1, 0), "y > 1");
        invalidEdit(() -> original.withButton("A", 0, 0, 0.039, 0.1, 0), "small width");
        invalidEdit(() -> original.withButton("A", 0, 0, 0.1, 0.401, 0), "large height");
        invalidEdit(() -> original.withButton("A", 0, 0, 0.1, 0.1, 0.051), "corner > half side");
        invalidEdit(() -> original.withButton("A", Double.NaN, 0, 0.1, 0.1, 0), "NaN edit");
        invalidEdit(() -> original.withButton("missing", 0, 0, 0.1, 0.1, 0), "unknown edit id");
        invalidEdit(() -> original.withStick("left", 0, 0, 0.6, 1, 0.1), "overlapping sticks");
        invalidEdit(() -> original.withStick("left", 0.8, 0, 0.4, 1, 0.1), "activation out of bounds");
        invalidEdit(() -> original.withStick("left", 0, 0, 0.5, 1, 0.301), "stick radius");
        invalidEdit(() -> original.withStick("left", 0, 0, 0, 1, 0.1), "empty activation");
        invalidEdit(() -> original.withStick("missing", 0, 0, 0.5, 1, 0.1), "unknown stick edit id");
        TouchLayout resized = original.withStick("left", 0.1, 0.1, 0.3, 0.8, 0.12);
        near(resized.stick("left").activation.x, 0.1, "stick edit");
        near(original.stick("left").activation.x, 0, "original stick unchanged");
        TouchLayout touching = original.withStick("left", 0, 0, 0.5, 1, 0.1);
        check(touching.sticks.size() == 2, "touching edges are allowed");
        byte[] before = original.jsonBytes();
        invalidEdit(() -> original.withStick("left", 0, 0, 0.9, 1, 0.1), "failed transaction");
        check(Arrays.equals(before, original.jsonBytes()), "failed edit preserves serialized model");
        TouchLayout named = edited.copyAs("user-layout", "布局\"\\测试😀");
        Files.write(Path.of(args[1]), named.jsonBytes());
        TouchLayout reread = TouchLayout.read(named.jsonBytes());
        check(reread.identifier.equals("user-layout") && reread.name.equals(named.name), "UTF-8 escaped name roundtrip");
        near(reread.button("A").cornerRadius, 0.05, "roundtrip corner");
        near(reread.button("A").x, 0.99, "roundtrip edited centre");
        invalidEdit(() -> original.copyAs("default", "x"), "reserved default identity");
        invalidEdit(() -> original.copyAs("touch", "x"), "reserved touch identity");
        invalidEdit(() -> original.copyAs("x", "bad\nname"), "control in edited name");
        // Foundation controlCharacterSet is Cc + Cf, including supplementary format characters.
        for (String control : new String[] {"\u00ad", "\u202e", "\udb40\udc01"})
            invalidEdit(() -> original.copyAs("x", "bad" + control), "format character in edited name");
        invalidEdit(() -> original.copyAs("x", "\ud800"), "unpaired surrogate");
        invalidEdit(() -> original.copyAs("x", "x".repeat(65)), "name > 64 UTF-16 units");
        // Editor save, same numbers as iOS runtime/tests/t_vpad_custom.m: safe rect (47, 0, 750x369).
        java.util.Map<String, double[]> centres = new java.util.HashMap<>();
        centres.put("A", new double[] {422, 184.5});
        centres.put("B", new double[] {10, 500});
        TouchLayout mine = original.withButtonCenters(centres, 47, 0, 750, 369, "mine", "我的布局");
        near(mine.button("A").x, 0.5, "drag centre x = (422 - 47) / 750");
        near(mine.button("A").y, 0.5, "drag centre y = 184.5 / 369");
        near(mine.button("B").x, 0, "drag centre clamped left");
        near(mine.button("B").y, 1, "drag centre clamped bottom");
        near(mine.button("A").width, original.button("A").width, "drag keeps width");
        near(mine.button("A").cornerRadius, original.button("A").cornerRadius, "drag keeps corner");
        near(mine.button("X").x, original.button("X").x, "undragged button kept");
        near(mine.stick("left").radius, original.stick("left").radius, "drag keeps sticks");
        check(mine.identifier.equals("mine") && mine.name.equals("我的布局"), "drag saves user identity");
        near(original.button("A").x, read(reference).button("A").x, "drag leaves original unchanged");
        TouchLayout.Rect dragged = mine.frameForButton("A", 47, 0, 750, 369);
        near(dragged.x + dragged.width / 2, 422, "saved centre projects back x");
        near(dragged.y + dragged.height / 2, 184.5, "saved centre projects back y");
        java.util.Map<String, double[]> unknown = new java.util.HashMap<>();
        unknown.put("missing", new double[] {1, 1});
        invalidEdit(() -> original.withButtonCenters(unknown, 47, 0, 750, 369, "mine", "x"), "drag unknown id");
        java.util.Map<String, double[]> nan = new java.util.HashMap<>();
        nan.put("A", new double[] {Double.NaN, 1});
        invalidEdit(() -> original.withButtonCenters(nan, 47, 0, 750, 369, "mine", "x"), "drag NaN centre");
        invalidEdit(() -> original.withButtonCenters(centres, 47, 0, 0, 369, "mine", "x"), "drag zero viewport");
        invalidEdit(() -> original.withButtonCenters(centres, 47, 0, 750, 369, "default", "x"), "drag reserved identity");
        // Hit test: inside the A frame, inside only with slop, and empty space.
        check("A".equals(mine.buttonAt(422, 184.5, 47, 0, 750, 369, 0)), "hit A centre");
        double edge = dragged.x + dragged.width + 5;
        check(!"A".equals(mine.buttonAt(edge, 184.5, 47, 0, 750, 369, 0)), "outside A without slop");
        check("A".equals(mine.buttonAt(edge, 184.5, 47, 0, 750, 369, 6)), "slop grows A frame");
        invalidEdit(() -> mine.buttonAt(Double.NaN, 0, 47, 0, 750, 369, 0), "NaN hit point");
        invalidEdit(() -> mine.buttonAt(0, 0, 47, 0, 750, 369, -1), "negative slop");
        // F01 tolerance applies only to cross-field boundaries, not scalar ranges.
        original.withButton("A", 0, 0, 0.1, 0.1, 0.05 + 0.5e-12);
        original.withStick("left", 0, 0, 0.5 + 0.5e-12, 1, 0.1);
        invalidEdit(() -> original.withStick("left", 0, 0, 0.5 + 2e-12, 1, 0.1), "overlap above tolerance");
        invalidEdit(() -> original.withButton("A", 1 + 0.5e-12, 0, 0.1, 0.1, 0), "scalar has no tolerance");
        try { original.buttons.clear(); throw new AssertionError("mutable button list"); }
        catch (UnsupportedOperationException expected) { checks++; }
        try { original.sticks.clear(); throw new AssertionError("mutable stick list"); }
        catch (UnsupportedOperationException expected) { checks++; }
        System.out.println("TouchLayout: " + checks + " checks passed (F01 reference + edit/geometry/rejection)");
    }
}
